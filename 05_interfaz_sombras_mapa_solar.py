"""Entorno local de edificios, geometría 3D y mapa solar rectangular.

python 05_interfaz_sombras_mapa_solar.py
python 05_interfaz_sombras_mapa_solar.py --demo --no-interfaz --exportar resultados_sombras

Edificios rectangulares opacos sobre un terreno horizontal z=0. Eje x al este,
eje y al norte y eje z hacia arriba. El análisis corresponde a un punto receptor;
la fracción de posiciones bloqueadas no equivale a una pérdida energética.
"""

from __future__ import annotations

import argparse
import copy
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import unicodedata
import webbrowser
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import matplotlib
import numpy as np
import pandas as pd


MODO_SIN_INTERFAZ = any(k in sys.argv for k in ("--no-interfaz", "--salida", "--exportar"))
if MODO_SIN_INTERFAZ:
    matplotlib.use("Agg")

from matplotlib.figure import Figure


COLOR_SOL = "#E6A700"
COLOR_VISIBLE = "#168C5A"
COLOR_SOMBRA = "#C73535"
COLOR_TRAYECTORIA = "#B48724"


def leer_argumentos() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Muestra una máscara de sombras sobre un mapa solar rectangular."
    )
    parser.add_argument("--mascara", type=Path, help="Archivo CSV o Excel de la máscara.")
    parser.add_argument("--solar", type=Path, help="Archivo CSV o Excel de posiciones solares.")
    parser.add_argument("--salida", type=Path, help="Archivo PNG de salida.")
    parser.add_argument("--proyecto", type=Path, help="Proyecto JSON de sitio y edificios.")
    parser.add_argument("--exportar", type=Path, help="Carpeta de proyecto, tablas y figuras.")
    parser.add_argument(
        "--no-interfaz",
        action="store_true",
        help="Procesa los archivos sin abrir la ventana.",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Usa datos demostrativos sin cargar archivos.",
    )
    return parser.parse_args()


def nombre_normalizado(nombre: object) -> str:
    texto = str(nombre).strip().lower()
    texto = "".join(
        caracter
        for caracter in unicodedata.normalize("NFD", texto)
        if unicodedata.category(caracter) != "Mn"
    )
    for caracter in (" ", "-", "/", "(", ")", "[", "]"):
        texto = texto.replace(caracter, "_")
    while "__" in texto:
        texto = texto.replace("__", "_")
    return texto.strip("_")


def leer_tabla(ruta: Path) -> pd.DataFrame:
    ruta = ruta.expanduser().resolve()
    if not ruta.exists():
        raise FileNotFoundError(f"No se encontró el archivo: {ruta}")

    extension = ruta.suffix.lower()
    if extension == ".csv":
        tabla = pd.read_csv(ruta)
    elif extension in {".xlsx", ".xlsm"}:
        tabla = pd.read_excel(ruta)
    else:
        raise ValueError("El archivo debe tener extensión .csv, .xlsx o .xlsm.")

    if tabla.empty:
        raise ValueError(f"El archivo no contiene datos: {ruta.name}")

    tabla = tabla.copy()
    tabla.columns = [nombre_normalizado(columna) for columna in tabla.columns]
    return tabla


def buscar_columna(
    tabla: pd.DataFrame,
    alternativas: tuple[str, ...],
    descripcion: str,
) -> str:
    for alternativa in alternativas:
        if alternativa in tabla.columns:
            return alternativa
    opciones = ", ".join(alternativas)
    raise ValueError(
        f"Falta la columna de {descripcion}. Nombres admitidos: {opciones}."
    )


def preparar_mascara(tabla: pd.DataFrame) -> pd.DataFrame:
    columna_azimut = buscar_columna(
        tabla,
        ("azimuth", "azimut", "solar_azimuth", "gamma"),
        "azimut",
    )
    columna_elevacion = buscar_columna(
        tabla,
        (
            "obstruction_elevation",
            "elevacion_obstruccion",
            "elevacion_obstaculo",
            "horizon_elevation",
            "elevation",
            "elevacion",
        ),
        "elevación de obstrucción",
    )

    resultado = pd.DataFrame(
        {
            "azimuth": pd.to_numeric(tabla[columna_azimut], errors="coerce"),
            "obstruction_elevation": pd.to_numeric(
                tabla[columna_elevacion], errors="coerce"
            ),
        }
    )

    columna_id = next(
        (
            columna
            for columna in ("obstacle_id", "obstaculo", "id_obstaculo", "grupo")
            if columna in tabla.columns
        ),
        None,
    )
    if columna_id is not None:
        resultado["obstacle_id"] = tabla[columna_id].astype(str)

    resultado = resultado.dropna(subset=["azimuth", "obstruction_elevation"])
    if resultado.empty:
        raise ValueError("La máscara no contiene valores numéricos válidos.")
    if not resultado["obstruction_elevation"].between(0, 90).all():
        raise ValueError("obstruction_elevation debe estar entre 0° y 90°.")

    resultado["azimuth"] = resultado["azimuth"] % 360
    return resultado


def preparar_posiciones_solares(tabla: pd.DataFrame) -> pd.DataFrame:
    columna_azimut = buscar_columna(
        tabla,
        ("azimuth", "azimut", "solar_azimuth", "gamma_s"),
        "azimut solar",
    )
    columna_elevacion = buscar_columna(
        tabla,
        ("apparent_elevation", "elevation", "elevacion_aparente", "elevacion"),
        "elevación solar",
    )

    resultado = pd.DataFrame(
        {
            "azimuth": pd.to_numeric(tabla[columna_azimut], errors="coerce"),
            "apparent_elevation": pd.to_numeric(
                tabla[columna_elevacion], errors="coerce"
            ),
        }
    )

    columna_tiempo = next(
        (
            columna
            for columna in (
                "timestamp",
                "datetime",
                "time",
                "fecha_hora",
                "fecha",
            )
            if columna in tabla.columns
        ),
        None,
    )
    if columna_tiempo is not None:
        resultado["timestamp"] = pd.to_datetime(
            tabla[columna_tiempo], errors="coerce"
        )

    resultado = resultado.dropna(subset=["azimuth", "apparent_elevation"])
    resultado = resultado.loc[resultado["apparent_elevation"] > 0].copy()
    if resultado.empty:
        raise ValueError("No hay posiciones solares con elevación mayor que 0°.")
    if not resultado["apparent_elevation"].between(0, 90).all():
        raise ValueError("La elevación solar debe estar entre 0° y 90°.")

    resultado["azimuth"] = resultado["azimuth"] % 360
    return resultado


def interpolar_segmento(
    azimut: np.ndarray,
    elevacion: np.ndarray,
    rejilla: np.ndarray,
) -> np.ndarray:
    orden = np.argsort(azimut)
    x = np.asarray(azimut, dtype=float)[orden]
    y = np.asarray(elevacion, dtype=float)[orden]
    x_unico, indices = np.unique(x, return_index=True)
    x = x_unico
    y = y[indices]

    perfil = np.zeros_like(rejilla, dtype=float)
    if len(x) == 1:
        indice = int(np.argmin(np.abs(rejilla - x[0])))
        perfil[indice] = y[0]
        return perfil

    if x.max() - x.min() > 180:
        x = np.where(x < 180, x + 360, x)
        orden = np.argsort(x)
        x = x[orden]
        y = y[orden]
        rejilla_extendida = np.where(rejilla < 180, rejilla + 360, rejilla)
        dentro = (rejilla_extendida >= x.min()) & (rejilla_extendida <= x.max())
        perfil[dentro] = np.interp(rejilla_extendida[dentro], x, y)
    else:
        dentro = (rejilla >= x.min()) & (rejilla <= x.max())
        perfil[dentro] = np.interp(rejilla[dentro], x, y)
    return perfil


def crear_perfil_mascara(
    mascara: pd.DataFrame,
) -> tuple[np.ndarray, np.ndarray]:
    rejilla = np.linspace(0, 360, 361)

    if "obstacle_id" in mascara.columns:
        perfiles = []
        for _, grupo in mascara.groupby("obstacle_id"):
            perfiles.append(
                interpolar_segmento(
                    grupo["azimuth"].to_numpy(),
                    grupo["obstruction_elevation"].to_numpy(),
                    rejilla,
                )
            )
        perfil = np.max(np.vstack(perfiles), axis=0)
    else:
        azimut = mascara["azimuth"].to_numpy()
        elevacion = mascara["obstruction_elevation"].to_numpy()
        cobertura_amplia = len(mascara) >= 180 and np.ptp(azimut) >= 300
        if cobertura_amplia:
            agrupada = (
                mascara.groupby("azimuth", as_index=False)["obstruction_elevation"]
                .max()
                .sort_values("azimuth")
            )
            x = agrupada["azimuth"].to_numpy()
            y = agrupada["obstruction_elevation"].to_numpy()
            if x[0] > 0:
                x = np.insert(x, 0, 0.0)
                y = np.insert(y, 0, y[-1])
            if x[-1] < 360:
                x = np.append(x, 360.0)
                y = np.append(y, y[0])
            perfil = np.interp(rejilla, x, y)
        else:
            perfil = interpolar_segmento(azimut, elevacion, rejilla)

    perfil[0] = max(perfil[0], perfil[-1])
    perfil[-1] = perfil[0]
    return rejilla, perfil


def clasificar_posiciones(
    posiciones: pd.DataFrame,
    rejilla: np.ndarray,
    perfil: np.ndarray,
) -> pd.DataFrame:
    resultado = posiciones.copy()
    resultado["obstruction_elevation"] = np.interp(
        resultado["azimuth"].to_numpy(),
        rejilla,
        perfil,
    )
    resultado["sombreado"] = (
        resultado["apparent_elevation"]
        <= resultado["obstruction_elevation"]
    )
    return resultado


def dibujar_trayectorias(ax, posiciones: pd.DataFrame) -> None:
    if "timestamp" not in posiciones.columns:
        return

    datos = posiciones.dropna(subset=["timestamp"]).copy()
    if datos.empty:
        return
    datos["dia"] = datos["timestamp"].dt.date

    for _, trayectoria in datos.groupby("dia"):
        trayectoria = trayectoria.sort_values("timestamp")
        elevacion = trayectoria["apparent_elevation"].copy()
        saltos = trayectoria["azimuth"].diff().abs() > 180
        elevacion.loc[saltos] = np.nan
        ax.plot(
            trayectoria["azimuth"],
            elevacion,
            color=COLOR_TRAYECTORIA,
            linewidth=0.8,
            alpha=0.22,
            zorder=2,
        )


def crear_figura(
    mascara: pd.DataFrame,
    posiciones: pd.DataFrame,
    titulo: str = "Mapa solar rectangular con máscara de sombras",
) -> tuple[Figure, pd.DataFrame]:
    rejilla, perfil = crear_perfil_mascara(mascara)
    clasificacion = clasificar_posiciones(posiciones, rejilla, perfil)

    figura = Figure(figsize=(11, 7), constrained_layout=True)
    ax = figura.add_subplot(111)

    ax.fill_between(
        rejilla,
        0,
        perfil,
        color=COLOR_SOMBRA,
        alpha=0.25,
        label="Región obstruida",
        zorder=1,
    )
    ax.plot(
        rejilla,
        perfil,
        color=COLOR_SOMBRA,
        linewidth=2.2,
        label="Máscara de horizonte",
        zorder=3,
    )
    dibujar_trayectorias(ax, clasificacion)

    visibles = clasificacion.loc[~clasificacion["sombreado"]]
    sombreados = clasificacion.loc[clasificacion["sombreado"]]
    ax.scatter(
        visibles["azimuth"],
        visibles["apparent_elevation"],
        s=14,
        color=COLOR_VISIBLE,
        alpha=0.62,
        label="Sol visible",
        zorder=4,
    )
    if not sombreados.empty:
        ax.scatter(
            sombreados["azimuth"],
            sombreados["apparent_elevation"],
            s=22,
            color=COLOR_SOMBRA,
            edgecolors="white",
            linewidths=0.35,
            alpha=0.9,
            label="Sol bloqueado",
            zorder=5,
        )

    ax.set_xlim(0, 360)
    ax.set_ylim(0, 90)
    ax.set_xticks(np.arange(0, 361, 45))
    ax.set_xticklabels(
        ["N\n0°", "45°", "E\n90°", "135°", "S\n180°", "225°", "O\n270°", "315°", "N\n360°"]
    )
    ax.set_yticks(np.arange(0, 91, 10))
    ax.set_xlabel("Azimut, convención pvlib [°]")
    ax.set_ylabel("Elevación aparente [°]")
    ax.set_title(titulo, fontsize=14, fontweight="bold")
    ax.grid(True, alpha=0.28)
    ax.legend(loc="upper right", fontsize=9)

    return figura, clasificacion


def crear_datos_demo() -> tuple[pd.DataFrame, pd.DataFrame]:
    azimut = np.arange(0, 361, dtype=float)
    perfil = np.zeros_like(azimut)
    perfil = np.maximum(
        perfil,
        np.where(
            (azimut >= 70) & (azimut <= 115),
            18 + 10 * np.sin(np.pi * (azimut - 70) / 45),
            0,
        ),
    )
    perfil = np.maximum(
        perfil,
        np.where(
            (azimut >= 250) & (azimut <= 295),
            15 + 10 * np.sin(np.pi * (azimut - 250) / 45),
            0,
        ),
    )
    perfil = np.maximum(
        perfil,
        np.where(
            (azimut >= 305) & (azimut <= 345),
            25 + 14 * np.sin(np.pi * (azimut - 305) / 40),
            0,
        ),
    )
    mascara = pd.DataFrame(
        {"azimuth": azimut, "obstruction_elevation": perfil}
    )

    from pvlib.location import Location

    sitio = Location(
        latitude=19.04,
        longitude=-98.20,
        tz="America/Mexico_City",
        altitude=2160,
        name="Puebla",
    )
    fechas = ("2026-03-20", "2026-06-21", "2026-09-22", "2026-12-21")
    bloques = []
    for fecha in fechas:
        tiempos = pd.date_range(
            f"{fecha} 05:00",
            f"{fecha} 19:30",
            freq="5min",
            tz=sitio.tz,
        )
        posicion = sitio.get_solarposition(tiempos).reset_index()
        posicion = posicion.rename(columns={posicion.columns[0]: "timestamp"})
        bloques.append(posicion[["timestamp", "azimuth", "apparent_elevation"]])
    posiciones = pd.concat(bloques, ignore_index=True)
    posiciones = posiciones.loc[posiciones["apparent_elevation"] > 0].copy()
    return mascara, posiciones




def proyecto_ejemplo() -> dict:
    """Esquema de tres edificios en Puebla; coordenadas en metros, centros en planta."""
    return {
        "version": 2, "latitude": 19.04, "longitude": -98.20, "altitude": 2160.,
        "timezone": "America/Mexico_City", "start": "2026-01-01", "end": "2026-12-31",
        "minutes": 15, "receiver_x": 0., "receiver_y": 0., "receiver_z": 1.5,
        "surface_tilt": 20., "surface_azimuth": 180., "vertical_scale": 3.,
        "radiation_source": "Cielo despejado", "albedo": .20,
        "weather_data": [], "weather_key": "",
        "nasa_metadata": {},
        "sun_date": "2026-03-20", "sun_hour": 9.,
        "buildings": [
            {"id": "B1", "name": "Edificio 1", "x": -220., "y": 150.,
             "width": 40., "depth": 300., "height": 25., "rotation": 0.},
            {"id": "B2", "name": "Edificio 2", "x": 225., "y": 225.,
             "width": 50., "depth": 50., "height": 35., "rotation": 0.},
            {"id": "B3", "name": "Edificio 3", "x": 200., "y": -320.,
             "width": 400., "depth": 40., "height": 20., "rotation": 0.},
        ],
        "mask_mode": "Edificios", "solar_mode": "pvlib",
        "imported_mask": [], "imported_solar": [],
        "example_note": (
            "Ejemplo inspirado en el esquema de clase. Alturas: 25, 35 y 20 m. "
            "E1: cara este x=-200 m, longitud norte-sur 300 m. "
            "E2: caras próximas x=200 m, y=200 m; planta 50 × 50 m. "
            "E3: cara norte y=-300 m, ancho este-oeste 400 m. "
            "Espesores de E1 y E3: 40 m supuestos; planta de E2 cuadrada supuesta. "
            "Receptor a 1.5 m y orientación inicial 20°/180° supuestos. "
            "Todos los edificios parten del terreno horizontal z=0."
        ),
    }


def proyecto_nuevo():
    c = proyecto_ejemplo()
    c.update(buildings=[], example_note="")
    return c


def numero(valor, nombre, minimo, maximo):
    try:
        resultado = float(valor)
    except (TypeError, ValueError):
        raise ValueError(f"{nombre}: escriba un número con punto decimal.") from None
    if not np.isfinite(resultado) or not minimo <= resultado <= maximo:
        raise ValueError(f"{nombre}: el valor debe estar entre {minimo:g} y {maximo:g}.")
    return resultado


def hora_a_decimal(texto):
    """Convierte una hora civil HH:MM a horas decimales."""
    partes = texto.strip().split(":")
    if (len(partes) != 2 or any(len(p) != 2 or not p.isascii() or not p.isdigit() for p in partes)
            or not 0 <= int(partes[0]) <= 23 or not 0 <= int(partes[1]) <= 59):
        raise ValueError("Hora local: use HH:MM, entre 00:00 y 23:59.")
    return int(partes[0]) + int(partes[1])/60


def hora_a_texto(hora):
    minutos = min(1439, max(0, round(float(hora)*60)))
    return f"{minutos//60:02d}:{minutos%60:02d}"


def validar_proyecto(proyecto):
    c = proyecto_ejemplo()
    c.update(copy.deepcopy(proyecto))
    if c["version"] != 2:
        raise ValueError("Versión de proyecto no admitida.")
    rangos = {
        "latitude": (-89.9, 89.9), "longitude": (-180, 180), "altitude": (-500, 9000),
        "receiver_x": (-1e6, 1e6), "receiver_y": (-1e6, 1e6), "receiver_z": (0, 10000),
        "surface_tilt": (0, 90), "surface_azimuth": (0, 360), "sun_hour": (0, 23.9999),
        "vertical_scale": (1, 10),
        "albedo": (0, 1),
    }
    for k, limites in rangos.items():
        c[k] = numero(c[k], k, *limites)
    c["vertical_scale"] = 3.
    ZoneInfo(c["timezone"])
    if float(c["minutes"]) not in (5, 10, 15, 30, 60):
        raise ValueError("Intervalo: seleccione 5, 10, 15, 30 o 60 minutos.")
    c["minutes"] = int(float(c["minutes"]))
    for k in ("start", "end", "sun_date"):
        t = pd.Timestamp(c[k])
        if pd.isna(t) or t.tzinfo or t != t.normalize():
            raise ValueError(f"{k}: use una fecha AAAA-MM-DD sin hora.")
        c[k] = t.strftime("%Y-%m-%d")
    if c["start"] > c["end"] or (pd.Timestamp(c["end"])-pd.Timestamp(c["start"])).days > 731:
        raise ValueError("Seleccione fechas ordenadas y un periodo máximo de dos años.")
    if c["mask_mode"] not in ("Edificios", "Archivo") or c["solar_mode"] not in ("pvlib", "Archivo"):
        raise ValueError("Origen de máscara o posiciones no admitido.")
    if c["radiation_source"] not in ("Cielo despejado", "Datos meteorológicos", "NASA POWER"):
        raise ValueError("Seleccione una fuente de radiación válida.")
    if c["radiation_source"] == "NASA POWER" and c["minutes"] != 60:
        raise ValueError("NASA POWER utiliza intervalos de 60 minutos.")
    ids = set()
    for i, b in enumerate(c["buildings"]):
        b["id"] = str(b.get("id", f"B{i+1}"))
        b["name"] = str(b["name"]).strip()
        if not b["name"] or b["id"] in ids:
            raise ValueError("Cada edificio necesita nombre y un identificador único.")
        ids.add(b["id"])
        for k in ("x", "y"):
            b[k] = numero(b[k], f"{b['name']}: {k}", -1e6, 1e6)
        for k in ("width", "depth", "height"):
            b[k] = numero(b[k], f"{b['name']}: {k}", .01, 10000)
        b["rotation"] = numero(b["rotation"], f"{b['name']}: giro", -360, 360) % 360
        local = (np.array([c["receiver_x"]-b["x"], c["receiver_y"]-b["y"]]) @ matriz_giro(b))
        dentro = abs(local[0]) < b["width"]/2-1e-8 and abs(local[1]) < b["depth"]/2-1e-8
        if dentro and c["receiver_z"] < b["height"]-1e-8:
            raise ValueError(f"El receptor está dentro del volumen de {b['name']}. Mueva el edificio o eleve el receptor hasta la cubierta.")
        borde = abs(local[0]) <= b["width"]/2+1e-8 and abs(local[1]) <= b["depth"]/2+1e-8
        if borde and c["receiver_z"] < b["height"]-1e-8:
            raise ValueError(f"El receptor toca una fachada de {b['name']}. Sepárelo o elévelo hasta la cubierta.")
    # Conserva la geometría relativa de proyectos con un origen anterior distinto.
    for b in c["buildings"]:
        b["x"] -= c["receiver_x"]
        b["y"] -= c["receiver_y"]
    c["receiver_x"] = c["receiver_y"] = 0.
    return c


def matriz_giro(b):
    """Giro horario visto en planta; 0°: ancho en E-O, fondo en N-S."""
    t = np.radians(b["rotation"])
    return np.array([[np.cos(t), np.sin(t)], [-np.sin(t), np.cos(t)]])


def vertices_edificio(b):
    locales = np.array([[-1, -1], [1, -1], [1, 1], [-1, 1]], dtype=float)
    locales *= np.array([b["width"]/2, b["depth"]/2])
    return locales @ matriz_giro(b).T + [b["x"], b["y"]]


def tabla_vertices(c):
    columnas = ["building_id", "building", "vertex", "east_m", "north_m", "height_m",
                "delta_east_m", "delta_north_m", "delta_height_m", "horizontal_distance_m",
                "distance_3d_m", "azimuth", "elevation"]
    filas = []
    for b in c["buildings"]:
        for i, (x, y) in enumerate(vertices_edificio(b), 1):
            dx, dy, dz = x-c["receiver_x"], y-c["receiver_y"], b["height"]-c["receiver_z"]
            r = np.hypot(dx, dy)
            filas.append([b["id"], b["name"], f"V{i}", x, y, b["height"], dx, dy, dz,
                          r, np.hypot(r, dz), np.degrees(np.arctan2(dx, dy)) % 360 if r > 1e-10 else np.nan,
                          np.degrees(np.arctan2(dz, r)) if r or dz else 0.])
    return pd.DataFrame(filas, columns=columnas)


def perfil_edificios(azimuth, c, separados=False):
    """Horizonte exacto por intersección rayo-rectángulo en planta, sin interpolar vértices."""
    az = np.atleast_1d(np.asarray(azimuth, dtype=float))
    direcciones = np.column_stack([np.sin(np.radians(az)), np.cos(np.radians(az))])
    perfiles = {}
    for b in c["buildings"]:
        perfil = np.zeros(len(az))
        dh = b["height"]-c["receiver_z"]
        if dh > 1e-9:
            giro = matriz_giro(b)
            origen = np.array([c["receiver_x"]-b["x"], c["receiver_y"]-b["y"]]) @ giro
            rayos = direcciones @ giro
            cerca = np.zeros(len(az))
            lejos = np.full(len(az), np.inf)
            valido = np.ones(len(az), dtype=bool)
            for eje, semiancho in enumerate((b["width"]/2, b["depth"]/2)):
                no_paralelo = np.abs(rayos[:, eje]) > 1e-12
                valido &= no_paralelo | (abs(origen[eje]) <= semiancho+1e-9)
                t1 = np.full(len(az), -np.inf)
                t2 = np.full(len(az), np.inf)
                a = (-semiancho-origen[eje])/rayos[no_paralelo, eje]
                z = (semiancho-origen[eje])/rayos[no_paralelo, eje]
                t1[no_paralelo], t2[no_paralelo] = np.minimum(a, z), np.maximum(a, z)
                cerca, lejos = np.maximum(cerca, t1), np.minimum(lejos, t2)
            valido &= (lejos >= cerca-1e-8) & (lejos > 1e-8) & np.isfinite(cerca)
            perfil[valido] = np.degrees(np.arctan2(dh, np.maximum(cerca[valido], 1e-10)))
        perfiles[b["id"]] = perfil
    total = np.max(np.vstack(list(perfiles.values())), axis=0) if perfiles else np.zeros(len(az))
    return (total, perfiles) if separados else total




_WEATHER_CACHE = {}


NASA_PARAMETROS = {"ALLSKY_SFC_SW_DWN": "ghi", "ALLSKY_SFC_SW_DNI": "dni", "ALLSKY_SFC_SW_DIFF": "dhi"}
NASA_API = "https://power.larc.nasa.gov/api/temporal/hourly/point"


def carpeta_cache_nasa():
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home()/"AppData"/"Local"))
    elif sys.platform == "darwin":
        base = Path.home()/"Library"/"Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home()/".cache"))
    return base/"EnergiaRenovable"/"NASA_POWER"


def respuesta_nasa(url, carpeta, forzar=False):
    """Conserva la respuesta original y comprueba su integridad antes de reutilizarla."""
    carpeta = Path(carpeta)
    carpeta.mkdir(parents=True, exist_ok=True)
    clave = hashlib.sha256(url.encode()).hexdigest()
    archivo, registro = carpeta/f"{clave}.json", carpeta/f"{clave}.fuente.json"
    if not forzar and archivo.is_file() and registro.is_file():
        try:
            cuerpo = archivo.read_bytes()
            fuente = json.loads(registro.read_text(encoding="utf-8"))
            if fuente["url"] == url and fuente["sha256"] == hashlib.sha256(cuerpo).hexdigest():
                return json.loads(cuerpo), dict(fuente, cache=True)
        except (ValueError, KeyError, OSError):
            pass
    try:
        peticion = Request(url, headers={"User-Agent": "EnergiaRenovable-Curso/1.0", "Accept": "application/json"})
        with urlopen(peticion, timeout=60) as respuesta:
            cuerpo = respuesta.read()
    except HTTPError as error:
        detalle = error.read().decode("utf-8", errors="replace")[:700]
        raise ValueError(f"NASA POWER no pudo entregar el periodo solicitado (HTTP {error.code}). {detalle}") from None
    except (URLError, TimeoutError, OSError) as error:
        raise ValueError(f"No se pudo conectar con NASA POWER. Revise la conexión e intente nuevamente. {error}") from None
    try:
        datos = json.loads(cuerpo)
    except ValueError:
        raise ValueError("NASA POWER devolvió una respuesta que no es JSON válido.") from None
    if not isinstance(datos, dict) or "properties" not in datos:
        raise ValueError("La respuesta de NASA POWER no contiene una serie de radiación válida.")
    fuente = {"url": url, "downloaded_at_utc": pd.Timestamp.now(tz="UTC").isoformat(),
              "sha256": hashlib.sha256(cuerpo).hexdigest(), "bytes": len(cuerpo)}
    archivo.write_bytes(cuerpo)
    registro.write_text(json.dumps(fuente, indent=2, ensure_ascii=False), encoding="utf-8")
    return datos, dict(fuente, cache=False)


def tabla_respuesta_nasa(datos):
    if datos.get("header", {}).get("time_standard") != "UTC":
        raise ValueError("NASA POWER: se esperaba una respuesta horaria en UTC.")
    columnas = {}
    for nombre, destino in NASA_PARAMETROS.items():
        try:
            serie = datos["properties"]["parameter"][nombre]
            unidad = datos["parameters"][nombre]["units"]
        except (KeyError, TypeError):
            raise ValueError(f"NASA POWER no entregó {nombre} o sus unidades.") from None
        normal = unidad.lower().replace(" ", "").replace("^", "").replace("²", "2").replace("-hr", "h")
        # Energía en una hora dividida entre una hora: Wh/m² y W/m² tienen el mismo valor numérico.
        if normal in ("wh/m2", "w/m2"):
            factor = 1.
        elif normal == "kwh/m2":
            factor = 1000.
        else:
            raise ValueError(f"NASA POWER: unidad horaria no admitida para {nombre}: {unidad}.")
        columnas[destino] = pd.to_numeric(pd.Series(serie), errors="coerce")*factor
    tabla = pd.DataFrame(columnas)
    try:
        tabla.index = pd.to_datetime(tabla.index, format="%Y%m%d%H", utc=True)
    except ValueError:
        raise ValueError("NASA POWER devolvió fechas horarias inválidas.") from None
    if tabla.index.has_duplicates:
        raise ValueError("NASA POWER devolvió horas duplicadas.")
    return tabla.sort_index()


def descargar_nasa_power(c, carpeta=None, progreso=None, forzar=False):
    """Descarga GHI, DNI y DHI históricas y prepara intervalos locales de una hora."""
    c = copy.deepcopy(c)
    c.update(minutes=60, radiation_source="NASA POWER", solar_mode="pvlib")
    c = validar_proyecto(c)
    inicio = pd.Timestamp(c["start"]).tz_localize(c["timezone"])
    fin = (pd.Timestamp(c["end"])+pd.Timedelta(days=1)).tz_localize(c["timezone"])
    if pd.Timestamp(c["end"]) >= pd.Timestamp.now(tz=c["timezone"]).tz_localize(None).normalize():
        raise ValueError("Seleccione un periodo histórico completo. NASA POWER no proporciona pronósticos ni días futuros.")
    primero = inicio.tz_convert("UTC").floor("h")
    ultimo = fin.tz_convert("UTC").ceil("h")
    if primero < pd.Timestamp("2001-01-01", tz="UTC"):
        raise ValueError("Seleccione un periodo cubierto por los datos horarios de NASA POWER, desde 2001.")
    carpeta = Path(carpeta) if carpeta is not None else carpeta_cache_nasa()
    tablas, fuentes, cabeceras = [], [], []
    actual = primero.normalize()
    final = (ultimo-pd.Timedelta(nanoseconds=1)).normalize()
    while actual <= final:
        cierre = min(final, pd.Timestamp(year=actual.year, month=12, day=31, tz="UTC"))
        parametros = {"parameters": ",".join(NASA_PARAMETROS), "community": "RE",
                      "longitude": c["longitude"], "latitude": c["latitude"], "start": actual.strftime("%Y%m%d"),
                      "end": cierre.strftime("%Y%m%d"), "format": "JSON", "time-standard": "UTC"}
        if progreso:
            progreso(f"NASA POWER: obteniendo {actual:%Y-%m-%d} a {cierre:%Y-%m-%d}…")
        datos, fuente = respuesta_nasa(NASA_API+"?"+urlencode(parametros), carpeta, forzar)
        tablas.append(tabla_respuesta_nasa(datos))
        fuentes.append(fuente)
        cabeceras.append(datos.get("header", {}))
        actual = cierre+pd.Timedelta(days=1)
    tabla = pd.concat(tablas).sort_index()
    if tabla.index.has_duplicates:
        raise ValueError("La descarga contiene horas duplicadas entre bloques.")
    esperadas = pd.date_range(primero, ultimo, freq="h", inclusive="left")
    tabla = tabla.reindex(esperadas)
    malos = (~np.isfinite(tabla) | (tabla < 0)).any(axis=1)
    if malos.any():
        falta = tabla.index[np.flatnonzero(malos.to_numpy())[0]]
        for fuente in fuentes:
            clave = hashlib.sha256(fuente["url"].encode()).hexdigest()
            (carpeta/f"{clave}.fuente.json").unlink(missing_ok=True)
        raise ValueError(f"NASA POWER no tiene datos completos: falta una componente válida en {falta}. "
                         "No se rellenaron huecos. Elija un periodo disponible o vuelva a intentar más adelante.")
    inicios = pd.date_range(inicio, fin, freq="60min", inclusive="left")
    # La integral acumulada conserva la energía también en zonas con desfases UTC de media hora.
    limites = inicios.tz_convert("UTC").append(pd.DatetimeIndex([fin.tz_convert("UTC")]))
    horas = (limites-primero).total_seconds().to_numpy()/3600
    if not np.allclose(np.diff(horas), 1.):
        raise ValueError("El periodo incluye un cambio horario fraccionario. Use una zona de desfase fijo para mantener intervalos de una hora.")
    acumulada = np.vstack([np.zeros(3), np.cumsum(tabla[["ghi", "dni", "dhi"]].to_numpy(), axis=0)])
    valores = np.column_stack([np.diff(np.interp(horas, np.arange(len(acumulada)), acumulada[:, j]))/np.diff(horas)
                               for j in range(3)])
    limpia = pd.DataFrame(np.maximum(valores, 0.), columns=["ghi", "dni", "dhi"])
    limpia.insert(0, "timestamp", inicios.map(lambda t: t.isoformat()))
    c["weather_data"] = limpia.to_dict("records")
    c["weather_key"] = hashlib.sha256(json.dumps(c["weather_data"], sort_keys=True).encode()).hexdigest()
    c["nasa_metadata"] = {"provider": "NASA POWER", "latitude": c["latitude"], "longitude": c["longitude"],
        "timezone": c["timezone"], "start": c["start"], "end": c["end"], "interval_minutes": 60,
        "units": "W/m²", "timestamp": "inicio del intervalo", "parameters": NASA_PARAMETROS,
        "sources": fuentes, "headers": cabeceras, "cache_folder": str(carpeta),
        "reference": "https://power.larc.nasa.gov/docs/services/api/temporal/hourly/",
        "time_alignment": "Promedios horarios UTC integrados sobre los intervalos locales; sin resolución subhoraria."}
    identificador = hashlib.sha256(json.dumps({k: c[k] for k in ("latitude", "longitude", "timezone", "start", "end")}, sort_keys=True).encode()).hexdigest()
    limpia.to_csv(carpeta/f"{identificador}.csv", index=False)
    (carpeta/f"{identificador}.proveniencia.json").write_text(json.dumps(c["nasa_metadata"], indent=2, ensure_ascii=False), encoding="utf-8")
    return c


def validar_proyecto_interfaz(proyecto):
    c = validar_proyecto(proyecto)
    if c["mask_mode"] != "Edificios":
        raise ValueError("Esta versión utiliza la geometría de los edificios. Abra un proyecto de edificios; no se admiten máscaras externas.")
    return c


def tabla_meteorologica(c):
    if c["radiation_source"] == "NASA POWER":
        meta = c.get("nasa_metadata", {})
        if (not meta or any(meta.get(k) != c[k] for k in ("latitude", "longitude", "timezone"))):
            raise ValueError("Descargue NASA POWER para las coordenadas y zona horaria actuales desde la pestaña Radiación.")
    if not c["weather_data"]:
        raise ValueError("Importe la tabla meteorológica en la pestaña Radiación o use Cielo despejado.")
    clave = (c["weather_key"] or hashlib.sha256(json.dumps(c["weather_data"], sort_keys=True).encode()).hexdigest(),
             c["timezone"])
    if clave in _WEATHER_CACHE:
        return _WEATHER_CACHE[clave].copy()
    d = pd.DataFrame(c["weather_data"]).copy()
    d.columns = [nombre_normalizado(k) for k in d.columns]
    if "timestamp" not in d or "ghi" not in d:
        raise ValueError("Radiación: se requieren timestamp y ghi. dni y dhi son opcionales, juntas.")
    fechas = []
    for i, valor in enumerate(d["timestamp"], 2):
        try:
            t = pd.Timestamp(valor)
            if pd.isna(t):
                raise ValueError()
            fechas.append(t.tz_localize(c["timezone"]) if t.tzinfo is None else t.tz_convert(c["timezone"]))
        except Exception:
            raise ValueError(f"Radiación: fecha inválida en la fila {i}.") from None
    d.index = pd.DatetimeIndex(fechas)
    if d.index.has_duplicates:
        raise ValueError("Radiación: hay fechas duplicadas.")
    if ("dni" in d) != ("dhi" in d):
        raise ValueError("Incluya dni y dhi juntas, o ninguna para estimarlas con Erbs.")
    columnas = ["ghi"]+([ "dni", "dhi"] if "dni" in d else [])
    for k in columnas:
        d[k] = pd.to_numeric(d[k], errors="coerce")
        malos = ~np.isfinite(d[k]) | (d[k] < 0)
        if malos.any():
            i = int(np.flatnonzero(malos.to_numpy())[0])+2
            raise ValueError(f"Radiación: fila {i}, {k} debe ser un número no negativo en W/m².")
    d = d[columnas].sort_index()
    if len(_WEATHER_CACHE) >= 4:
        _WEATHER_CACHE.pop(next(iter(_WEATHER_CACHE)))
    _WEATHER_CACHE[clave] = d
    return d.copy()


def completar_componentes(clima, posicion, tiempos):
    from pvlib import irradiance
    clima = clima.copy()
    if "dni" not in clima:
        erbs = irradiance.erbs(clima["ghi"].to_numpy(dtype=float),
                              posicion["zenith"].to_numpy(dtype=float), tiempos)
        clima["dni"] = np.asarray(erbs["dni"])
        clima["dhi"] = np.asarray(erbs["dhi"])
    noche = posicion["apparent_elevation"].to_numpy(dtype=float) <= 0
    clima.loc[noche, ["ghi", "dni", "dhi"]] = 0.
    return clima


@lru_cache(maxsize=16)
def _trayectoria_diaria(latitude, longitude, altitude, timezone, fecha):
    from pvlib.location import Location
    inicio = pd.Timestamp(fecha).tz_localize(timezone)
    fin = (pd.Timestamp(fecha)+pd.Timedelta(days=1)).tz_localize(timezone)
    tiempos = pd.date_range(inicio, fin, freq="5min", inclusive="left")
    p = Location(latitude, longitude, timezone, altitude).get_solarposition(tiempos)
    p = p.loc[p["apparent_elevation"] > 0, ["azimuth", "apparent_elevation"]].copy()
    p.insert(0, "timestamp", p.index)
    return p.reset_index(drop=True)


def trayectoria_diaria(c):
    return _trayectoria_diaria(c["latitude"], c["longitude"], c["altitude"],
                               c["timezone"], c["sun_date"]).copy()


def fraccion_cielo_visible(c):
    az = np.arange(.5, 360., 1.)
    el = np.radians(np.arange(.5, 90., 1.))
    beta = np.radians(c["surface_tilt"])
    gamma = np.radians(az-c["surface_azimuth"])
    cos_inc = np.cos(beta)*np.sin(el)[None, :]+np.sin(beta)*np.cos(gamma)[:, None]*np.cos(el)[None, :]
    peso = np.maximum(cos_inc, 0.)*np.cos(el)[None, :]
    visible = np.degrees(el)[None, :] > horizonte(az, c)[:, None]
    return float(np.clip(np.sum(peso*visible)/np.sum(peso), 0., 1.))


def calcular_poa(d, c, cielo_visible):
    from pvlib import irradiance
    d = d.copy()
    elev = d["apparent_elevation"].to_numpy(dtype=float)
    poa = irradiance.get_total_irradiance(
        surface_tilt=c["surface_tilt"], surface_azimuth=c["surface_azimuth"],
        solar_zenith=90-elev, solar_azimuth=d["azimuth"].to_numpy(dtype=float),
        dni=d["dni"].to_numpy(dtype=float), ghi=d["ghi"].to_numpy(dtype=float),
        dhi=d["dhi"].to_numpy(dtype=float), albedo=c["albedo"], model="isotropic")
    for k in ("poa_global", "poa_direct", "poa_sky_diffuse", "poa_ground_diffuse"):
        d[k] = np.asarray(poa[k])
    d.loc[elev <= 0, ["poa_global","poa_direct","poa_sky_diffuse","poa_ground_diffuse"]] = 0.
    d["poa_direct_shaded"] = d["poa_direct"]*np.where(d["sombreado"], 0., 1.)
    d["poa_sky_diffuse_shaded"] = d["poa_sky_diffuse"]*cielo_visible
    d["poa_loss"] = d["poa_direct"]*np.where(d["sombreado"], 1., 0.)+d["poa_sky_diffuse"]*(1-cielo_visible)
    d["poa_shaded"] = np.maximum(0., d["poa_global"]-d["poa_loss"])
    d["poa_loss_percent"] = np.where(d["poa_global"] > 1e-9,
        100*d["poa_loss"]/d["poa_global"].replace(0., np.nan), np.nan)
    if not np.isfinite(d[["poa_global", "poa_shaded", "poa_loss"]]).all().all():
        raise ValueError("El cálculo de irradiancia produjo datos no finitos. Revise las entradas.")
    return d


def balance_radiacion(d, c, cielo_visible):
    columnas = ["poa_global", "poa_shaded", "poa_loss", "poa_direct",
                "poa_direct_shaded", "poa_sky_diffuse", "poa_sky_diffuse_shaded", "poa_ground_diffuse"]
    if not {"ghi", "dni", "dhi", "timestamp", "interval_start"} <= set(d.columns) or d.empty:
        return None
    d = calcular_poa(d, c, cielo_visible)
    horas = c["minutes"]/60
    diario = d.set_index("timestamp")[columnas].resample("D").sum()*horas/1000
    calendario = pd.date_range(pd.Timestamp(c["start"]).tz_localize(c["timezone"]),
                                pd.Timestamp(c["end"]).tz_localize(c["timezone"]), freq="D")
    diario = diario.reindex(calendario, fill_value=0.)
    diario.columns = [k+"_kwh_m2" for k in columnas]
    mensual = diario.resample("MS").sum()
    total = diario.sum()
    return {"series": d, "daily": diario, "monthly": mensual,
            "baseline_kwh_m2": float(total["poa_global_kwh_m2"]),
            "received_kwh_m2": float(total["poa_shaded_kwh_m2"]),
            "loss_kwh_m2": float(total["poa_loss_kwh_m2"]),
            "loss_percent": float(100*total["poa_loss_kwh_m2"]/total["poa_global_kwh_m2"])
                if total["poa_global_kwh_m2"] > 1e-9 else None}


def validar_filtro(valores, c):
    f = dict(valores)
    for k in ("start", "end"):
        t = pd.Timestamp(f[k])
        if pd.isna(t) or t.tzinfo or t != t.normalize():
            raise ValueError("Filtro: escriba las fechas como AAAA-MM-DD.")
        f[k] = t.strftime("%Y-%m-%d")
    if not c["start"] <= f["start"] <= f["end"] <= c["end"]:
        raise ValueError("Las fechas del filtro deben estar dentro del periodo calculado.")
    f["from_hour"] = hora_a_decimal(f["from_time"])
    f["to_hour"] = 24. if f["to_time"] == "24:00" else hora_a_decimal(f["to_time"])
    if f["from_hour"] >= f["to_hour"]:
        raise ValueError("Filtro: la hora inicial debe ser anterior a la final; use 24:00 para terminar el día.")
    return f


def filtrar_modelo(modelo, filtro):
    """Selecciona las muestras por fecha y hora local, sin modificar el cálculo completo."""
    if filtro is None:
        return modelo
    d = modelo["solar"]
    if "timestamp" not in d or (len(d) and d["timestamp"].isna().any()):
        raise ValueError("El filtro requiere posiciones solares con fecha y hora.")
    c = modelo["config"]
    resultado = dict(modelo)
    if len(d):
        tiempos = pd.to_datetime(d["timestamp"], utc=True).dt.tz_convert(c["timezone"])
        dias = tiempos.dt.strftime("%Y-%m-%d")
        horas = tiempos.dt.hour+tiempos.dt.minute/60+tiempos.dt.second/3600
        seleccion = ((dias >= filtro["start"]) & (dias <= filtro["end"])
                     & (horas >= filtro["from_hour"]) & (horas < filtro["to_hour"]))
        d = d.loc[seleccion].copy()
    resultado["solar"] = d
    resultado["view_filter"] = dict(filtro)
    resultado["full_radiation"] = modelo.get("radiation")
    r = modelo.get("radiation")
    if r is not None:
        columnas = list(r["daily"].columns)
        campos = [k.removesuffix("_kwh_m2") for k in columnas]
        calendario = pd.date_range(filtro["start"], filtro["end"], freq="D", tz=c["timezone"])
        if len(d):
            diario = d.set_index("timestamp")[campos].resample("D").sum()*c["minutes"]/60000
            diario.columns = columnas
            diario = diario.reindex(calendario, fill_value=0.)
        else:
            diario = pd.DataFrame(0., index=calendario, columns=columnas)
        total = diario.sum()
        resultado["radiation"] = {
            "series": d, "daily": diario, "monthly": diario.resample("MS").sum(),
            "baseline_kwh_m2": float(total["poa_global_kwh_m2"]),
            "received_kwh_m2": float(total["poa_shaded_kwh_m2"]),
            "loss_kwh_m2": float(total["poa_loss_kwh_m2"]),
            "loss_percent": float(100*total["poa_loss_kwh_m2"]/total["poa_global_kwh_m2"])
                if total["poa_global_kwh_m2"] > 1e-9 else None}
    return resultado


def figura_radiacion(m, figura=None):
    import matplotlib.dates as mdates
    fig = figura if figura is not None else Figure(figsize=(11, 7), constrained_layout=True)
    fig.clear()
    ax, bx = fig.subplots(2, 1)
    r = m.get("radiation")
    if r is None:
        for eje in (ax, bx):
            eje.axis("off")
        ax.text(.5, .5, "Calcule el recurso solar para comparar irradiancia y pérdidas.",
                ha="center", transform=ax.transAxes)
        return fig
    c = m["config"]
    filtro = m.get("view_filter")
    fecha = c["sun_date"]
    if filtro and not filtro["start"] <= fecha <= filtro["end"]:
        fecha = filtro["start"]
    dia = r["series"].loc[r["series"]["timestamp"].dt.strftime("%Y-%m-%d") == fecha] if len(r["series"]) else r["series"]
    if len(dia):
        marcador = "." if len(dia) <= 12 else None
        ax.plot(dia["timestamp"], dia["poa_global"], label="Plano sin obstáculos", color="#D69B00", lw=1.8, marker=marcador)
        ax.plot(dia["timestamp"], dia["poa_shaded"], label="Plano con obstáculos", color="#197A69", lw=1.8, marker=marcador)
        ax.fill_between(dia["timestamp"], dia["poa_shaded"], dia["poa_global"],
                         alpha=.23, color=COLOR_SOMBRA, label="Pérdida por sombras")
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=ZoneInfo(c["timezone"])))
        ax.set(xlabel="Hora local", ylabel="Irradiancia POA [W/m²]")
        ax.legend(fontsize=9)
        ax.grid(alpha=.2)
    else:
        ax.text(.5, .5, "No hay muestras solares diurnas para este día y horario.",
                ha="center", transform=ax.transAxes)
        ax.axis("off")
    mensual = r["monthly"]
    x = np.arange(len(mensual))
    bx.bar(x, mensual["poa_shaded_kwh_m2"], color="#197A69", label="Recibida")
    bx.bar(x, mensual["poa_loss_kwh_m2"], bottom=mensual["poa_shaded_kwh_m2"],
            color=COLOR_SOMBRA, alpha=.7, label="Pérdida por sombras")
    bx.set_xticks(x, mensual.index.strftime("%Y-%m"), rotation=40)
    bx.set(ylabel="Irradiación [kWh/m²]", title="Balance mensual de la selección" if filtro else "Balance mensual")
    bx.set_ylim(0, max(1., float(mensual["poa_global_kwh_m2"].max())*1.12))
    bx.legend(fontsize=9)
    bx.grid(axis="y", alpha=.2)
    fig.suptitle(fecha, fontsize=11)
    return fig


def plantilla_meteorologica(c):
    copia = copy.deepcopy(c)
    copia["radiation_source"] = "Cielo despejado"
    d = calcular_posiciones(copia)
    inicio = pd.Timestamp(c["start"]).tz_localize(c["timezone"])
    fin = (pd.Timestamp(c["end"])+pd.Timedelta(days=1)).tz_localize(c["timezone"])
    indice = pd.date_range(inicio, fin, freq=f"{c['minutes']}min", inclusive="left")
    tabla = d.set_index("interval_start")[["ghi", "dni", "dhi"]].reindex(indice, fill_value=0.)
    tabla.insert(0, "timestamp", indice.map(lambda t: t.isoformat()))
    return tabla.reset_index(drop=True)


def calcular_posiciones(c):
    from pvlib.location import Location
    inicio = pd.Timestamp(c["start"]).tz_localize(c["timezone"])
    fin = (pd.Timestamp(c["end"])+pd.Timedelta(days=1)).tz_localize(c["timezone"])
    inicios = pd.date_range(inicio, fin, freq=f"{c['minutes']}min", inclusive="left")
    tiempos = inicios
    tiempos = tiempos+pd.Timedelta(minutes=c["minutes"]/2)
    sitio = Location(c["latitude"], c["longitude"], c["timezone"], c["altitude"])
    posicion = sitio.get_solarposition(tiempos)
    d = posicion[["azimuth", "apparent_elevation"]].copy()
    if c["radiation_source"] == "Cielo despejado":
        clima = sitio.get_clearsky(tiempos, model="ineichen", solar_position=posicion)
    else:
        clima = tabla_meteorologica(c).reindex(inicios)
        if clima["ghi"].isna().any():
            falta = inicios[clima["ghi"].isna().to_numpy()][0]
            raise ValueError(f"El recurso no cubre todos los intervalos. Falta {falta}. Ajuste periodo o intervalo.")
        clima.index = tiempos
        clima = completar_componentes(clima, posicion, tiempos)
    for k in ("ghi", "dni", "dhi"):
        d[k] = clima[k]
    d["interval_start"] = inicios
    d = d.loc[d["apparent_elevation"] > 0].copy()
    d.insert(0, "timestamp", d.index)
    return d.reset_index(drop=True)


def incidencia_panel(azimuth, elevation, c):
    beta, gamma = np.radians(c["surface_tilt"]), np.radians(c["surface_azimuth"])
    az, el = np.radians(azimuth), np.radians(elevation)
    coseno = np.cos(beta)*np.sin(el)+np.sin(beta)*np.cos(el)*np.cos(az-gamma)
    coseno = np.where(np.abs(coseno) < 1e-12, 0., coseno)
    return np.degrees(np.arccos(np.clip(coseno, -1, 1))), np.maximum(0., coseno)


def sol_instante(c):
    from pvlib.location import Location
    tiempo = (pd.Timestamp(c["sun_date"])+pd.Timedelta(hours=c["sun_hour"])).tz_localize(c["timezone"])
    sitio = Location(c["latitude"], c["longitude"], c["timezone"], c["altitude"])
    p = sitio.get_solarposition(pd.DatetimeIndex([tiempo])).iloc[0]
    aoi, factor = incidencia_panel(p["azimuth"], p["apparent_elevation"], c)
    resultado = {"timestamp": tiempo, "azimuth": float(p["azimuth"]),
            "apparent_elevation": float(p["apparent_elevation"]), "aoi": float(aoi),
            "beam_projection": float(factor) if p["apparent_elevation"] > 0 else 0.}
    if c["radiation_source"] == "Cielo despejado":
        clima = sitio.get_clearsky(pd.DatetimeIndex([tiempo]), model="ineichen")
    elif c["weather_data"]:
        clima = tabla_meteorologica(c).reindex(pd.DatetimeIndex([tiempo.floor(f"{c['minutes']}min")]))
        clima.index = pd.DatetimeIndex([tiempo])
        if clima["ghi"].isna().any():
            return resultado
        clima = completar_componentes(clima, p.to_frame().T.set_axis(clima.index), clima.index)
    else:
        return resultado
    for k in ("ghi", "dni", "dhi"):
        resultado[k] = float(clima[k].iloc[0]) if p["apparent_elevation"] > 0 else 0.
    return resultado


def mascara_archivo(c):
    if not c["imported_mask"]:
        raise ValueError("La máscara importada está vacía. Importe datos o seleccione Edificios.")
    return preparar_importacion("Máscara angular", pd.DataFrame(c["imported_mask"]), c)


def horizonte(az, c):
    if c["mask_mode"] == "Edificios":
        return perfil_edificios(az, c)
    x, y = crear_perfil_mascara(mascara_archivo(c))
    return np.interp(az, x, y)


def analizar_geometria(c, posiciones=None):
    c = validar_proyecto(c)
    az = np.linspace(0, 360, 1441)
    vertices = tabla_vertices(c)
    perfil = horizonte(az, c)
    if posiciones is None:
        posiciones = pd.DataFrame(columns=["timestamp", "azimuth", "apparent_elevation"])
    d = posiciones.copy()
    d["obstruction_elevation"] = horizonte(d["azimuth"].to_numpy(dtype=float), c)
    d["sombreado"] = d["apparent_elevation"] <= d["obstruction_elevation"]
    aoi, proyeccion = incidencia_panel(d["azimuth"], d["apparent_elevation"], c)
    d["aoi"] = aoi
    d["in_front"] = proyeccion > 1e-10
    d["beam_projection"] = proyeccion
    d["beam_factor_shaded"] = proyeccion*np.where(d["sombreado"], 0., 1.)
    sol = sol_instante(c)
    sol["obstruction_elevation"] = float(horizonte([sol["azimuth"]], c)[0])
    sol["sombreado"] = 0 < sol["apparent_elevation"] <= sol["obstruction_elevation"]
    sol["in_front"] = sol["beam_projection"] > 1e-10
    camino = trayectoria_diaria(c)
    camino["sombreado"] = camino["apparent_elevation"].to_numpy() <= horizonte(camino["azimuth"].to_numpy(), c)
    cielo_visible = fraccion_cielo_visible(c)
    if {"ghi", "dni", "dhi"} <= set(sol):
        instantaneo = calcular_poa(pd.DataFrame([sol]), c, cielo_visible).iloc[0]
        for k in ("poa_global", "poa_shaded", "poa_loss", "poa_loss_percent", "poa_direct_shaded",
                  "poa_sky_diffuse_shaded", "poa_ground_diffuse"):
            sol[k] = float(instantaneo[k])
    radiacion = balance_radiacion(d, c, cielo_visible)
    if radiacion is not None:
        d = radiacion["series"]
    return {"config": c, "vertices": vertices, "grid": az, "profile": perfil, "solar": d,
            "sun": sol, "day_path": camino, "sky_visibility": cielo_visible, "radiation": radiacion}


def dibujar_geometria(modelo, building_id=None, vertex="V1", figura=None, vista=None):
    from matplotlib.patches import Polygon
    from matplotlib.ticker import MaxNLocator
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection, Line3DCollection
    fig = figura if figura is not None else Figure(figsize=(12, 7), constrained_layout=True)
    fig.clear()
    ax = fig.add_subplot(121)
    bx = fig.add_subplot(122, projection="3d")
    c = modelo["config"]
    p = np.array([c["receiver_x"], c["receiver_y"], c["receiver_z"]])
    limites = [p[:2]]
    colores = ["#4682B4", "#369C8F", "#B87E40", "#9370B8", "#6A8B45"]
    for i, b in enumerate(c["buildings"]):
        xy = vertices_edificio(b)
        limites.extend(xy)
        color = colores[i % len(colores)]
        seleccionado = b["id"] == building_id
        ax.add_patch(Polygon(xy, facecolor=color, alpha=.4, edgecolor="#17344d" if seleccionado else color,
                             linewidth=2.5 if seleccionado else 1))
        ax.text(b["x"], b["y"], f"{b['name']}\nh={b['height']:g} m", ha="center", va="center", fontsize=8)
        abajo = np.column_stack([xy, np.zeros(4)])
        arriba = np.column_stack([xy, np.full(4, b["height"])])
        caras = [arriba, abajo]+[np.array([abajo[j], abajo[(j+1)%4], arriba[(j+1)%4], arriba[j]]) for j in range(4)]
        bx.add_collection3d(Poly3DCollection(caras, facecolors=color, alpha=.33, edgecolors=color, linewidths=.8))
        bx.text(b["x"], b["y"], b["height"], b["name"], fontsize=8)
        if seleccionado:
            for j, (x, y) in enumerate(xy, 1):
                ax.text(x, y, f" V{j}", fontsize=9, fontweight="bold")
                bx.text(x, y, b["height"], f"V{j}", fontsize=8)
    limites = np.asarray(limites)
    minimo, maximo = limites.min(axis=0), limites.max(axis=0)
    span = max(20., float(np.max(maximo-minimo)))
    centro = (minimo+maximo)/2
    radio = .64*span
    top = max([p[2], 5]+[b["height"] for b in c["buildings"]])
    ax.scatter(*p[:2], s=60, c="black", marker="x", zorder=9)
    ax.annotate(" Receptor", p[:2], fontsize=9)
    bx.scatter(*p, s=40, c="black")
    tabla = modelo["vertices"]
    escogido = tabla.loc[(tabla["building_id"] == building_id) & (tabla["vertex"] == vertex)]
    if len(escogido):
        v = escogido.iloc[0]
        destino = np.array([v.east_m, v.north_m, v.height_m])
        pie = np.array([v.east_m, v.north_m, p[2]])
        ax.plot([p[0], destino[0]], [p[1], destino[1]], color="#C73535", lw=1.5)
        ax.scatter(destino[0], destino[1], color="#C73535", zorder=10)
        bx.plot(*np.array([p, destino]).T, color="#C73535", lw=2)
        bx.plot(*np.array([p, pie, destino]).T, color="#C73535", linestyle="--", lw=1)
    # Símbolo de orientación: tamaño gráfico, no dimensiones físicas de un panel.
    beta, gamma = np.radians(c["surface_tilt"]), np.radians(c["surface_azimuth"])
    normal = np.array([np.sin(beta)*np.sin(gamma), np.sin(beta)*np.cos(gamma), np.cos(beta)])
    e1 = np.array([np.cos(gamma), -np.sin(gamma), 0.])
    e2 = np.cross(normal, e1)
    tam = min(span*.025, max(.1, p[2]*.8/max(np.sin(beta), .01)))
    panel = np.array([p+tam*(a*e1+b*e2) for a, b in [(-1,-1),(1,-1),(1,1),(-1,1)]])
    bx.add_collection3d(Poly3DCollection([panel], facecolors="#244E87", edgecolors="navy", alpha=.85))
    for paso in (-.5, 0., .5):
        bx.plot(*np.array([p+tam*(paso*e1-e2), p+tam*(paso*e1+e2)]).T, color="#A5D8FF", lw=.5)
    bx.plot(*np.array([p-tam*e1, p+tam*e1]).T, color="#A5D8FF", lw=.5)
    bx.quiver(*p, *(normal*span*.1), color="navy", arrow_length_ratio=.2)
    horizontal = np.array([np.sin(gamma), np.cos(gamma)])
    ax.annotate("", xy=p[:2]+horizontal*span*.13, xytext=p[:2],
                arrowprops={"arrowstyle": "->", "color": "navy", "lw": 2})
    camino = modelo.get("day_path")
    radio_solar = span*.32
    if camino is not None and len(camino) > 1:
        aa = np.radians(camino["azimuth"].to_numpy())
        ee = np.radians(camino["apparent_elevation"].to_numpy())
        puntos = p+radio_solar*np.column_stack([np.cos(ee)*np.sin(aa), np.cos(ee)*np.cos(aa), np.sin(ee)])
        segmentos = np.stack([puntos[:-1], puntos[1:]], axis=1)
        colores_sol = np.where(camino["sombreado"].to_numpy()[:-1], COLOR_SOMBRA, COLOR_SOL)
        bx.add_collection3d(Line3DCollection(segmentos, colors=colores_sol, linewidths=2.2))
        ax.plot(puntos[:, 0], puntos[:, 1], color=COLOR_SOL, lw=1.4, alpha=.8)
        for f in (.28, .7):
            j = min(len(puntos)-2, int(f*len(puntos)))
            vector = puntos[j+1]-puntos[j]
            bx.quiver(*puntos[j], *(vector*3), color=COLOR_SOL, arrow_length_ratio=.7, linewidth=1.5)
            ax.annotate("", xy=puntos[j+1, :2], xytext=puntos[max(0, j-3), :2],
                        arrowprops={"arrowstyle": "->", "color": COLOR_SOL, "lw": 1.5})
        for j, nombre in [(0, "Salida"), (len(puntos)-1, "Puesta")]:
            hora = camino["timestamp"].iloc[j].strftime("%H:%M")
            bx.scatter(*puntos[j], color=COLOR_SOL, s=15)
            bx.text(*puntos[j], f" {nombre} ≈{hora}", fontsize=8)
            ax.annotate(nombre, puntos[j, :2], xytext=(3, 7), textcoords="offset points", fontsize=8)
        top = max(top, float(puntos[:, 2].max()))
        limites = np.vstack([limites, puntos[:, :2]])
    sol = modelo["sun"]
    if sol["apparent_elevation"] > 0:
        a, e = np.radians(sol["azimuth"]), np.radians(sol["apparent_elevation"])
        direccion = np.array([np.cos(e)*np.sin(a), np.cos(e)*np.cos(a), np.sin(e)])
        vector = direccion*radio_solar
        color_sol = COLOR_SOMBRA if sol["sombreado"] else COLOR_SOL
        bx.plot(*np.array([p, p+vector]).T, color=color_sol, lw=1, linestyle="--", alpha=.6)
        bx.scatter(*(p+vector), c=color_sol, s=65)
        ax.annotate("", xy=p[:2]+direccion[:2]*span*.2, xytext=p[:2],
                    arrowprops={"arrowstyle": "->", "color": color_sol, "lw": 2})
        top = max(top, (p+vector)[2])
    minimo, maximo = limites.min(axis=0), limites.max(axis=0)
    centro = (minimo+maximo)/2
    radio = .64*max(20., float(np.max(maximo-minimo)))
    ax.set(xlim=(centro[0]-radio, centro[0]+radio), ylim=(centro[1]-radio, centro[1]+radio),
           xlabel="Este x [m]", ylabel="Norte y [m]")
    ax.set_aspect("equal", adjustable="box")
    ax.text(.03, .96, "N ↑", transform=ax.transAxes, va="top", fontweight="bold")
    ax.grid(alpha=.2)
    bx.set(xlim=(centro[0]-radio, centro[0]+radio), ylim=(centro[1]-radio, centro[1]+radio),
           zlim=(0, max(10., top*1.2)), xlabel="Este [m]", ylabel="Norte [m]", zlabel="Altura [m]",
           title="3D")
    bx.set_box_aspect((2*radio, 2*radio, max(10., top*1.2)*c["vertical_scale"]))
    bx.xaxis.set_major_locator(MaxNLocator(5))
    bx.yaxis.set_major_locator(MaxNLocator(5))
    bx.zaxis.set_major_locator(MaxNLocator(3))
    bx.view_init(elev=vista[0] if vista else 28, azim=vista[1] if vista else -55)
    bx.disable_mouse_rotation()
    # Detalle del símbolo para reconocer la orientación sin asignar dimensiones al receptor.
    detalle = fig.add_axes([.89, .80, .10, .15], projection="3d", label="panel_detalle", computed_zorder=False)
    detalle.set_facecolor("white")
    panel_local = np.array([a*e1+b*.65*e2 for a,b in [(-1,-1),(1,-1),(1,1),(-1,1)]])
    detalle.add_collection3d(Poly3DCollection([panel_local], facecolors="#154578", edgecolors="#9DD0F5", linewidths=1.2, zorder=1))
    for a in (-.5, 0., .5):
        detalle.plot(*np.array([a*e1-.65*e2,a*e1+.65*e2]).T, color="#B8E4FF", lw=.7, zorder=2)
    detalle.plot(*np.array([-e1,e1]).T, color="#B8E4FF", lw=.7, zorder=2)
    detalle.quiver(0,0,0,*(normal*.8), color="navy", arrow_length_ratio=.2)
    detalle.set(xlim=(-1.2,1.2), ylim=(-1.2,1.2), zlim=(-1,1.2))
    detalle.set_box_aspect((1,1,1))
    detalle.view_init(28, -55)
    detalle.set_axis_off()
    detalle.set_title(f"Panel\nβ {c['surface_tilt']:g}° · γ {c['surface_azimuth']:g}°", fontsize=8, pad=0)
    fig.suptitle(c["sun_date"], fontsize=10)
    return fig, ax, bx


def figura_triangulo(modelo, building_id=None, vertex="V1", figura=None):
    from matplotlib.patches import Arc
    fig = figura if figura is not None else Figure(figsize=(11, 6), constrained_layout=True)
    fig.clear()
    ax, planta, bx = fig.subplots(1, 3, gridspec_kw={"width_ratios": [1, 1, 1.1]})
    bx.axis("off")
    escogido = modelo["vertices"].loc[
        (modelo["vertices"]["building_id"] == building_id) & (modelo["vertices"]["vertex"] == vertex)]
    if escogido.empty:
        ax.axis("off")
        planta.axis("off")
        ax.text(.5, .5, "Seleccione un edificio y un vértice.", ha="center", transform=ax.transAxes)
        return fig
    v = escogido.iloc[0]
    d, h = v.horizontal_distance_m, v.delta_height_m
    ax.plot([0, d, d, 0], [0, 0, h, 0], lw=2, color="#3A6385", marker="o")
    escala = max(1., d, abs(h))
    ax.annotate(f"dₕ = {d:.2f} m", (d/2, 0), xytext=(0, -14), textcoords="offset points", ha="center", fontsize=9)
    ax.annotate(f"Δz = {h:.2f} m", (d, h/2), xytext=(-8, 0), textcoords="offset points", ha="right", fontsize=9,
                bbox={"facecolor": "white", "edgecolor": "none", "alpha": .8, "pad": 1})
    ax.annotate("Receptor", (0, 0), xytext=(4, 12), textcoords="offset points", fontsize=9)
    ax.annotate(f"{v.building} · {vertex}", (d, h), xytext=(-5, 12), textcoords="offset points", ha="right", fontsize=9)
    if d > 1e-9:
        ax.add_patch(Arc((0, 0), escala*.35, escala*.35, theta1=min(0, v.elevation),
                         theta2=max(0, v.elevation), color=COLOR_SOMBRA, lw=2))
    ax.set(xlim=(-escala*.1, max(d, 1)+escala*.4),
           ylim=(min(0, h)-escala*.15, max(h, 0)+escala*.25),
           xlabel="Distancia horizontal [m]", ylabel="Diferencia de altura [m]",
           title="Triángulo de elevación")
    ax.set_aspect("equal", adjustable="box")
    ax.grid(alpha=.2)
    dx, dy = float(v.delta_east_m), float(v.delta_north_m)
    planta.plot([0, dx, dx, 0], [0, 0, dy, 0], lw=2, color="#3A6385", marker="o")
    if abs(dx) > 1e-8:
        planta.annotate(f"Δx = {dx:.2f} m", (dx/2, 0), xytext=(0, -22), textcoords="offset points", ha="center", fontsize=9)
    if abs(dy) > 1e-8:
        planta.annotate(f"Δy = {dy:.2f} m", (dx, dy/2), xytext=(8, 0), textcoords="offset points", fontsize=9)
    else:
        planta.text(.03, .95, "Δy = 0 m", transform=planta.transAxes, va="top", fontsize=9)
    if abs(dx) <= 1e-8:
        planta.text(.03, .87, "Δx = 0 m", transform=planta.transAxes, va="top", fontsize=9)
    planta.annotate("Receptor", (0, 0), xytext=(-5, -12), textcoords="offset points", fontsize=9, ha="right")
    planta.annotate(vertex, (dx, dy), xytext=(6, 10), textcoords="offset points", fontsize=9)
    radio = max(1., d)*.28
    planta.annotate("N", (0, radio*1.65), xytext=(0, 4), textcoords="offset points", ha="center",
                    arrowprops=None, fontsize=9, fontweight="bold")
    planta.annotate("", (0, radio*1.65), (0, 0), arrowprops={"arrowstyle": "->", "color": "black"})
    if d > 1e-9:
        angulos = np.radians(np.linspace(0, float(v.azimuth), 150))
        planta.plot(radio*np.sin(angulos), radio*np.cos(angulos), color=COLOR_SOMBRA, lw=1.7)
        if len(angulos) > 1 and v.azimuth > 1e-6:
            planta.annotate("", (radio*np.sin(angulos[-1]), radio*np.cos(angulos[-1])),
                            (radio*np.sin(angulos[-3]), radio*np.cos(angulos[-3])),
                            arrowprops={"arrowstyle": "->", "color": COLOR_SOMBRA})
        medio = np.radians(float(v.azimuth)/2)
        planta.text(radio*1.25*np.sin(medio), radio*1.25*np.cos(medio), f"γ={v.azimuth:.2f}°", fontsize=9, ha="center", va="top",
                    bbox={"facecolor": "white", "edgecolor": "none", "alpha": .8, "pad": 1})
    margen = max(1., d)*.42
    planta.set(xlim=(min(0, dx)-margen, max(0, dx)+margen),
               ylim=(min(0, dy)-margen, max(radio*1.8, dy)+margen),
               xlabel="Este [m]", ylabel="Norte [m]", title="Triángulo de azimut")
    planta.set_aspect("equal", adjustable="box")
    planta.grid(alpha=.2)
    az = f"{v.azimuth:.3f}°" if np.isfinite(v.azimuth) else "indefinido (distancia horizontal cero)"
    texto = (
        f"{v.building} · {vertex}\n\n"
        f"Δx (este) = {v.delta_east_m:.3f} m\nΔy (norte) = {v.delta_north_m:.3f} m\n"
        f"Δz = H − hᵣ = {h:.3f} m\n\n"
        f"dₕ = √(Δx² + Δy²) = {d:.3f} m\n"
        f"d₃ᴅ = √(dₕ² + Δz²) = {v.distance_3d_m:.3f} m\n\n"
        f"Azimut γ = atan2(Δx, Δy) mod 360°\nγ = {az}\n\n"
        f"Elevación α = atan2(Δz, dₕ)\nα = {v.elevation:.3f}°\n\n"
        "N = 0° · E = 90° · S = 180° · O = 270°"
    )
    if h <= 0:
        texto += "\nEste edificio no sobresale del receptor."
    bx.text(.01, .98, texto, va="top", fontsize=9, linespacing=1.4)
    return fig


def figura_mapa_modelo(modelo, building_id=None, vertex="V1", figura=None):
    fig = figura if figura is not None else Figure(figsize=(11, 7), constrained_layout=True)
    fig.clear()
    ax = fig.add_subplot()
    c, az, perfil, d = modelo["config"], modelo["grid"], modelo["profile"], modelo["solar"]
    beta = np.radians(c["surface_tilt"])
    frontera = np.maximum(0., np.degrees(np.arctan2(
        -np.sin(beta)*np.cos(np.radians(az-c["surface_azimuth"])), np.cos(beta))))
    if np.max(frontera) > 1e-6:
        ax.fill_between(az, 0, frontera, color="#64748B", alpha=.12, hatch="//", label="Detrás del plano receptor")
    ax.fill_between(az, 0, perfil, color=COLOR_SOMBRA, alpha=.23, label="Obstrucción")
    ax.plot(az, perfil, color=COLOR_SOMBRA, lw=1.5, label="Horizonte")
    if len(d):
        if "timestamp" in d:
            dias = d["timestamp"].dt.strftime("%Y-%m-%d")
            elegidos = sorted(set(dias))
            salto = max(1, len(elegidos)//14)
            for dia in elegidos[::salto]:
                t = d.loc[dias == dia].sort_values("timestamp")
                elev = t["apparent_elevation"].to_numpy().copy()
                elev[np.r_[False, np.abs(np.diff(t["azimuth"])) > 180]] = np.nan
                ax.plot(t["azimuth"], elev, color="#BD9C50", alpha=.25, lw=.7)
        for seleccion, color, etiqueta in [
            (d["in_front"] & ~d["sombreado"], COLOR_VISIBLE, "Delante y visible"),
            (d["in_front"] & d["sombreado"], COLOR_SOMBRA, "Delante y bloqueado"),
            (~d["in_front"], "#64748B", "Sol detrás del plano"),
        ]:
            ax.scatter(d.loc[seleccion, "azimuth"], d.loc[seleccion, "apparent_elevation"],
                       c=color, s=5, alpha=.45, label=etiqueta, rasterized=True)
    else:
        ax.text(.5, .8, "No hay muestras solares en la selección." if modelo.get("view_filter") else "Calcule las trayectorias o importe posiciones solares.",
                transform=ax.transAxes, ha="center", bbox={"facecolor": "white", "alpha": .9})
    v = modelo["vertices"]
    seleccionado = v.loc[(v["building_id"] == building_id) & (v["vertex"] == vertex)]
    if c["mask_mode"] == "Edificios" and len(seleccionado) and np.isfinite(seleccionado.iloc[0].azimuth):
        q = seleccionado.iloc[0]
        ax.scatter(q.azimuth, q.elevation, marker="D", c="navy", s=50, zorder=10, label=f"{q.building} {vertex}")
    sol = modelo["sun"]
    filtro = modelo.get("view_filter")
    instante_en_filtro = (not filtro or (filtro["start"] <= sol["timestamp"].strftime("%Y-%m-%d") <= filtro["end"]
                         and filtro["from_hour"] <= sol["timestamp"].hour+sol["timestamp"].minute/60 < filtro["to_hour"]))
    if sol["apparent_elevation"] > 0 and instante_en_filtro:
        ax.scatter(sol["azimuth"], sol["apparent_elevation"], marker="*", c=COLOR_SOL,
                   edgecolors="black", s=160, zorder=11, label="Instante elegido")
    ax.set(xlim=(0, 360), ylim=(0, 90), xlabel="Azimut pvlib [°]", ylabel="Elevación aparente [°]",
           title=f"Mapa solar · máscara: {c['mask_mode']} · receptor β={c['surface_tilt']:g}°, γ={c['surface_azimuth']:g}°")
    ax.set_xticks(np.arange(0, 361, 45), ["N 0°","45°","E 90°","135°","S 180°","225°","O 270°","315°","N 360°"])
    ax.grid(alpha=.2)
    ax.legend(loc="upper right", fontsize=8)
    return fig


def exportar_consulta(modelo, carpeta):
    carpeta = Path(carpeta)/"consulta_filtrada"
    carpeta.mkdir(parents=True, exist_ok=True)
    (carpeta/"filtro.json").write_text(json.dumps(modelo["view_filter"], indent=2, ensure_ascii=False), encoding="utf-8")
    (carpeta/"resumen.txt").write_text(resumen_modelo(modelo), encoding="utf-8")
    modelo["solar"].to_csv(carpeta/"clasificacion_solar.csv", index=False)
    if modelo.get("radiation") is not None:
        modelo["radiation"]["daily"].to_csv(carpeta/"irradiacion_diaria.csv", index_label="fecha")
        modelo["radiation"]["monthly"].to_csv(carpeta/"irradiacion_mensual.csv", index_label="mes")
    for nombre, funcion in (("mapa_solar", figura_mapa_modelo), ("radiacion_y_perdidas", figura_radiacion)):
        fig = funcion(modelo)
        fig.savefig(carpeta/f"{nombre}.png", dpi=160)
        fig.clear()


def resumen_modelo(m):
    c, s = m["config"], m["sun"]
    noche = s["apparent_elevation"] <= 0
    estado = "Bajo el horizonte" if noche else "Bloqueado por edificio" if s["sombreado"] else "Visible"
    fuente = ("cielo despejado estimado" if c["radiation_source"] == "Cielo despejado" else
              "NASA POWER · estimaciones históricas horarias" if c["radiation_source"] == "NASA POWER" else "datos meteorológicos importados")
    def porcentaje(valor):
        return f"{valor:.2f} %" if valor is not None and np.isfinite(valor) else "—"
    texto = (
        f"Sitio: {c['latitude']:g}°, {c['longitude']:g}° · {c['timezone']}\n"
        f"Altura del receptor: {c['receiver_z']:g} m\n"
        f"Inclinación β={c['surface_tilt']:g}° · azimut γ={c['surface_azimuth']:g}° · edificios: {len(c['buildings'])}\n"
        f"Recurso: {fuente}\n\n"
        f"INSTANTE · {s['timestamp']:%Y-%m-%d %H:%M}\n"
        f"Azimut solar: {s['azimuth']:.2f}° · elevación: {s['apparent_elevation']:.2f}°\n"
        f"AOI: {s['aoi']:.2f}° · {estado} · {'delante del plano' if s['in_front'] and not noche else 'sin directa frontal'}\n"
    )
    if "poa_shaded" in s:
        texto += (
            f"Irradiancia sin obstáculos: {s['poa_global']:.2f} W/m²\n"
            f"Irradiancia recibida: {s['poa_shaded']:.2f} W/m²\n"
            f"Pérdida por sombras: {s['poa_loss']:.2f} W/m² · {porcentaje(s['poa_loss_percent'])}\n"
            f"Recibida: directa {s['poa_direct_shaded']:.2f} + cielo {s['poa_sky_diffuse_shaded']:.2f}"
            f" + terreno {s['poa_ground_diffuse']:.2f} W/m²\n"
        )
    else:
        texto += "Sin datos de radiación para el instante seleccionado.\n"
    r = m.get("radiation")
    if r is None:
        texto += "\nCalcule el recurso solar para obtener irradiaciones y pérdidas del periodo.\n"
    else:
        fecha = pd.Timestamp(c["sun_date"]).tz_localize(c["timezone"])
        if fecha in r["daily"].index:
            dia = r["daily"].loc[fecha]
            h0, h = dia["poa_global_kwh_m2"], dia["poa_shaded_kwh_m2"]
            perdida = max(0., h0-h)
            texto += (
                f"\nDÍA · {c['sun_date']}"+(" · horario filtrado\n" if m.get("view_filter") else "\n")+
                f"Irradiación sin obstáculos: {h0:.3f} kWh/m²\n"
                f"Irradiación recibida: {h:.3f} kWh/m²\n"
                f"Pérdida por sombras: {perdida:.3f} kWh/m² · {porcentaje(100*perdida/h0 if h0 > 1e-9 else None)}\n"
            )
        filtro = m.get("view_filter")
        titulo = (f"SELECCIÓN · {filtro['start']} a {filtro['end']} · {filtro['from_time']}–{filtro['to_time']}" if filtro
                  else f"PERIODO · {c['start']} a {c['end']}")
        texto += (
            f"\n{titulo}\n"
            f"Irradiación sin obstáculos: {r['baseline_kwh_m2']:.3f} kWh/m²\n"
            f"Irradiación recibida: {r['received_kwh_m2']:.3f} kWh/m²\n"
            f"Pérdida por sombras: {r['loss_kwh_m2']:.3f} kWh/m² · {porcentaje(r['loss_percent'])}\n"
        )
        if filtro:
            completo = m.get("full_radiation")
            texto += f"Muestras diurnas seleccionadas: {len(m['solar'])}. Horario aplicado cada día; fin excluido.\n"
            if completo:
                texto += (f"\nPERIODO COMPLETO · {c['start']} a {c['end']}\n"
                          f"Irradiación recibida: {completo['received_kwh_m2']:.3f} kWh/m²\n"
                          f"Pérdida por sombras: {completo['loss_kwh_m2']:.3f} kWh/m² · {porcentaje(completo['loss_percent'])}\n")
    texto += "\nPOA isotrópica: directa, cielo obstruido y terreno sin reducción. Cálculo puntual de radiación incidente."
    return texto


def exportar_modelo(modelo, carpeta, building_id=None, vertex="V1"):
    carpeta = Path(carpeta)
    carpeta.mkdir(parents=True, exist_ok=True)
    c = modelo["config"]
    (carpeta/"proyecto_sombras.json").write_text(json.dumps(c, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    (carpeta/"resumen_sombras.txt").write_text(resumen_modelo(modelo), encoding="utf-8")
    pd.DataFrame(c["buildings"], columns=["id","name","x","y","width","depth","height","rotation"]).to_csv(carpeta/"edificios.csv", index=False)
    modelo["vertices"].to_csv(carpeta/"vertices_angulos.csv", index=False)
    pd.DataFrame({"azimuth": modelo["grid"], "obstruction_elevation": modelo["profile"]}).to_csv(carpeta/"mascara_horizonte.csv", index=False)
    modelo["solar"].to_csv(carpeta/"clasificacion_solar.csv", index=False)
    modelo["day_path"].to_csv(carpeta/"trayectoria_dia.csv", index=False)
    if modelo.get("radiation") is not None:
        r = modelo["radiation"]
        r["daily"].to_csv(carpeta/"irradiacion_diaria.csv", index_label="fecha")
        r["monthly"].to_csv(carpeta/"irradiacion_mensual.csv", index_label="mes")
        if c["weather_data"]:
            pd.DataFrame(c["weather_data"]).to_csv(carpeta/"meteorologia_entrada.csv", index=False)
    if c["mask_mode"] == "Archivo":
        mascara_archivo(c).to_csv(carpeta/"mascara_importada.csv", index=False)
    if c["radiation_source"] == "NASA POWER" and c.get("nasa_metadata"):
        (carpeta/"fuentes_nasa_power.json").write_text(json.dumps(c["nasa_metadata"], ensure_ascii=False, indent=2), encoding="utf-8")
    graficas = [
        (dibujar_geometria(modelo, building_id, vertex)[0], "entorno_2d_3d"),
        (figura_triangulo(modelo, building_id, vertex), "triangulo_vertice"),
        (figura_mapa_modelo(modelo, building_id, vertex), "mapa_solar"),
        (figura_radiacion(modelo), "radiacion_y_perdidas"),
    ]
    for fig, nombre in graficas:
        fig.savefig(carpeta/f"{nombre}.png", dpi=160)
        fig.clear()


class AplicacionSombras:
    def __init__(self, proyecto=None, iniciar_calculo=True):
        import tkinter as tk
        from tkinter import ttk, filedialog, messagebox
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
        self.tk, self.ttk, self.fd, self.mb = tk, ttk, filedialog, messagebox
        self.Canvas, self.Toolbar = FigureCanvasTkAgg, NavigationToolbar2Tk
        self.raiz = tk.Tk()
        self.raiz.title("Análisis de Sombras - Energía Renovable")
        self.raiz.geometry("1460x880")
        self.raiz.minsize(1120, 720)
        self.config = validar_proyecto_interfaz(proyecto if proyecto is not None else proyecto_nuevo())
        carpeta_ejemplos = Path(__file__).resolve().parent/"proyectos_ejemplo"
        self.carpeta_proyectos = carpeta_ejemplos if carpeta_ejemplos.is_dir() else Path(__file__).resolve().parent
        self.variables, self.edificio_vars = {}, {}
        self.seleccion = self.config["buildings"][0]["id"] if self.config["buildings"] else None
        self.vertice = tk.StringVar(value="V1")
        self.estado = tk.StringVar(value="Introduzca el sitio y los edificios, o use Abrir proyecto para recuperar un caso guardado.")
        self.aviso = tk.StringVar()
        self._loading, self._pending = False, None
        self.trabajando, self.pendiente = False, None
        self.posiciones, self.solar_signature, self.modelo = None, None, None
        self.filtro = None
        self.nasa_trabajando, self.nasa_pendiente = False, None
        self.nasa_progreso = ""
        self.revision_proyecto = 0
        self._construir()
        self._cargar_campos()
        if self.config["solar_mode"] == "Archivo":
            self.posiciones = preparar_importacion("Posiciones solares", pd.DataFrame(self.config["imported_solar"]), self.config)
        self._actualizar_vistas()
        if iniciar_calculo and self.config["solar_mode"] == "pvlib":
            self.raiz.after(250, self.calcular_solar)

    def _construir(self):
        barra = self.ttk.Frame(self.raiz, padding=7)
        barra.pack(fill="x")
        for texto, orden in [("Nuevo proyecto", self.nuevo),
                             ("Abrir proyecto", self.abrir), ("Guardar proyecto", self.guardar),
                             ("Exportar todo", self.exportar)]:
            self.ttk.Button(barra, text=texto, command=orden).pack(side="left", padx=3)
        self.ttk.Label(self.raiz, textvariable=self.estado, padding=(10, 3), wraplength=1400).pack(fill="x")
        principal = self.ttk.Panedwindow(self.raiz, orient="horizontal")
        principal.pack(fill="both", expand=True, padx=8, pady=5)
        izquierda, derecha = self.ttk.Frame(principal, width=405), self.ttk.Frame(principal)
        principal.add(izquierda, weight=0)
        principal.add(derecha, weight=1)
        self.entradas = self.ttk.Notebook(izquierda)
        self.entradas.pack(fill="both", expand=True)
        self.pags = {}
        for nombre in ["Sitio y receptor", "Edificios", "Radiación"]:
            exterior = self.ttk.Frame(self.entradas)
            self.entradas.add(exterior, text=nombre)
            lienzo = self.tk.Canvas(exterior, highlightthickness=0, width=395)
            scroll = self.ttk.Scrollbar(exterior, orient="vertical", command=lienzo.yview)
            lienzo.configure(yscrollcommand=scroll.set)
            scroll.pack(side="right", fill="y")
            lienzo.pack(side="left", fill="both", expand=True)
            contenido = self.ttk.Frame(lienzo, padding=10)
            ventana = lienzo.create_window((0, 0), window=contenido, anchor="nw")
            contenido.bind("<Configure>", lambda e, cv=lienzo: cv.configure(scrollregion=cv.bbox("all")))
            lienzo.bind("<Configure>", lambda e, cv=lienzo, w=ventana: cv.itemconfigure(w, width=e.width))
            self.pags[nombre] = contenido
        sitio = self.pags["Sitio y receptor"]
        self._formulario(sitio, [
            ("latitude", "Latitud [°]"), ("longitude", "Longitud [°]"), ("altitude", "Altitud del sitio [m]"),
            ("timezone", "Zona horaria IANA"), ("start", "Inicio [AAAA-MM-DD]"), ("end", "Fin incluido [AAAA-MM-DD]"),
            ("minutes", "Intervalo [min]", [5,10,15,30,60]),
            ("receiver_z", "Receptor: altura z [m]"), ("surface_tilt", "Inclinación del panel β [°]"),
            ("surface_azimuth", "Azimut del panel γ [°]"),
        ], self.variables)
        self.ttk.Label(sitio, text="El receptor está en el origen de la planta (x=0, y=0).", wraplength=360).pack(anchor="w", pady=4)
        self.ttk.Button(sitio, text="Calcular trayectorias y radiación", command=self.calcular_solar).pack(fill="x", pady=8)
        self.ttk.Separator(sitio).pack(fill="x", pady=7)
        self.ttk.Label(sitio, text="Sol para inspeccionar la escena", font=("TkDefaultFont", 10, "bold")).pack(anchor="w")
        self._formulario(sitio, [("sun_date", "Fecha [AAAA-MM-DD]"), ("sun_time", "Hora local [HH:MM]")], self.variables)
        self.ttk.Label(sitio, wraplength=360, text=(
            "Formato de 24 horas; por ejemplo, 09:30 o 16:45. Este instante puede ser distinto del periodo de trayectorias.\n"
            "Norte 0° · Este 90° · Sur 180° · Oeste 270°.\n"
            "Inclinación: 0° horizontal, 90° vertical."
        )).pack(anchor="w", pady=8)
        edif = self.pags["Edificios"]
        self.lista = self.ttk.Treeview(edif, columns=["nombre", "altura"], show="headings", height=5, selectmode="browse")
        self.lista.heading("nombre", text="Edificio")
        self.lista.heading("altura", text="Altura [m]")
        self.lista.column("nombre", width=230)
        self.lista.column("altura", width=90)
        self.lista.pack(fill="x")
        self.lista.bind("<<TreeviewSelect>>", self._seleccionar_lista)
        acciones = self.ttk.Frame(edif)
        acciones.pack(fill="x", pady=6)
        for texto, orden in [("Agregar", self.agregar), ("Duplicar", self.duplicar), ("Quitar", self.quitar)]:
            self.ttk.Button(acciones, text=texto, command=orden).pack(side="left", padx=3)
        self._formulario(edif, [
            ("name", "Nombre"), ("x", "Centro: este x [m]"), ("y", "Centro: norte y [m]"),
            ("width", "Ancho edificio [m]"), ("depth", "Fondo edificio [m]"),
            ("height", "Altura desde z=0 [m]"), ("rotation", "Giro horario en planta [°]"),
        ], self.edificio_vars, edificio=True)
        fila = self.ttk.Frame(edif)
        fila.pack(fill="x", pady=8)
        self.ttk.Label(fila, text="Vértice superior:").pack(side="left")
        self.ttk.Combobox(fila, textvariable=self.vertice, values=["V1","V2","V3","V4"],
                          state="readonly", width=8).pack(side="left", padx=8)
        self.vertice.trace_add("write", self._programar_edicion)
        self.ttk.Label(edif, textvariable=self.aviso, foreground="#9A3412", wraplength=360).pack(fill="x", pady=5)
        self.ttk.Label(edif, wraplength=360, text=(
            "Giro 0°: ancho en este-oeste y fondo en norte-sur. Los edificios se apoyan en el terreno z=0.\n"
            "La posición corresponde al CENTRO de la planta. Las distancias a caras y vértices se calculan a partir de ella.\n"
            "Escriba números con punto decimal. Las vistas se actualizan al completar valores válidos."
        )).pack(anchor="w", pady=7)
        recurso = self.pags["Radiación"]
        self._formulario(recurso, [("radiation_source", "Recurso", ["Cielo despejado", "Datos meteorológicos", "NASA POWER"]),
                                  ("albedo", "Albedo del terreno [0–1]")], self.variables)
        self.weather_info = self.tk.StringVar(value=self.descripcion_recurso())
        self.ttk.Button(recurso, text="Importar radiación CSV / Excel", command=self.importar_radiacion).pack(fill="x", pady=6)
        self.ttk.Button(recurso, text="Guardar ejemplo de formato CSV / Excel", command=self.guardar_plantilla_radiacion).pack(fill="x", pady=6)
        self.boton_nasa = self.ttk.Button(recurso, text="Descargar y usar NASA POWER", command=self.descargar_nasa)
        self.boton_nasa.pack(fill="x", pady=6)
        self.progreso_nasa = self.ttk.Progressbar(recurso, mode="indeterminate")
        self.ttk.Label(recurso, wraplength=360, text=(
            "NASA POWER: descarga GHI, DNI y DHI para las coordenadas y fechas del proyecto. "
            "Utiliza intervalos de 60 minutos y requiere un periodo histórico disponible, por ejemplo 2025. "
            "Son estimaciones; no mediciones locales. La descarga se conserva para reutilizarla."
        )).pack(anchor="w", pady=4)
        self.ttk.Button(recurso, text="Calcular trayectorias y radiación", command=self.calcular_solar).pack(fill="x", pady=6)
        self.ttk.Label(recurso, textvariable=self.weather_info, wraplength=360, foreground="#245074").pack(anchor="w", pady=10)
        self.ttk.Label(recurso, wraplength=360, text=(
            "Cielo despejado: estimación con Ineichen; no representa la nubosidad real. "
            "Para estudiar un recurso medido o meteorológico validado, importe su tabla.\n\n"
            "Columnas: timestamp, ghi; dni y dhi opcionales, juntas. Irradiancias en W/m². "
            "timestamp indica el inicio del intervalo y los datos son sus valores medios. "
            "La serie debe cubrir todos los intervalos del periodo. CSV con comas y punto decimal; Excel en la primera hoja.\n\n"
            "El ejemplo de formato contiene una serie de cielo despejado calculada para el periodo elegido. "
            "Sirve para comprobar el formato; no son observaciones.\n\n"
            "POA: transposición isotrópica. Se bloquea la directa y se reduce la difusa por cielo obstruido. "
            "La reflejada del terreno se conserva. La pérdida se compara contra el MISMO plano sin obstáculos."
        )).pack(anchor="w", pady=6)
        self.ttk.Label(recurso, text="Albedos característicos", font=("TkDefaultFont", 10, "bold")).pack(anchor="w", pady=(12, 5))
        tabla_albedos = self.ttk.Treeview(recurso, columns=("superficie", "albedo"), show="headings", height=9, selectmode="none")
        tabla_albedos.heading("superficie", text="Superficie")
        tabla_albedos.heading("albedo", text="Albedo")
        tabla_albedos.column("superficie", width=245, minwidth=160)
        tabla_albedos.column("albedo", width=65, minwidth=55, anchor="center")
        from pvlib.albedo import SURFACE_ALBEDOS
        for superficie, clave in [("Asfalto", "asphalt"), ("Suelo", "soil"), ("Zona urbana", "urban"),
                                  ("Pasto", "grass"), ("Pasto fresco", "fresh grass"), ("Concreto", "concrete"),
                                  ("Arena", "sand"), ("Nieve", "snow"), ("Nieve fresca", "fresh snow")]:
            tabla_albedos.insert("", "end", values=(superficie, f"{SURFACE_ALBEDOS[clave]:.2f}"))
        tabla_albedos.pack(fill="x", pady=4)
        self.ttk.Label(recurso, wraplength=360, text=(
            "Valores de referencia de pvlib; varían con el estado de la superficie. "
            "Escriba el valor elegido en Albedo del terreno."
        )).pack(anchor="w", pady=4)
        self.ttk.Button(recurso, text="Consultar referencia de albedos", command=lambda: self.abrir_enlace(
            "https://pvlib-python.readthedocs.io/en/stable/_modules/pvlib/albedo.html")).pack(fill="x", pady=5)
        consulta = self.ttk.LabelFrame(derecha, text="Consulta de mapa solar, radiación y resultados", padding=5)
        consulta.pack(fill="x", pady=(0, 5))
        self.filtro_vars = {k: self.tk.StringVar(value=v) for k, v in (
            ("start", self.config["start"]), ("end", self.config["end"]),
            ("from_time", "00:00"), ("to_time", "24:00"))}
        for col, (key, etiqueta) in enumerate((("start", "Fecha desde"), ("end", "Fecha hasta"),
                                              ("from_time", "Hora desde"), ("to_time", "Hora hasta"))):
            self.ttk.Label(consulta, text=etiqueta).grid(row=0, column=col, sticky="w", padx=4)
            self.ttk.Entry(consulta, textvariable=self.filtro_vars[key], width=14 if col < 2 else 8).grid(row=1, column=col, sticky="ew", padx=4)
        self.ttk.Button(consulta, text="Aplicar filtro", command=self.aplicar_filtro).grid(row=1, column=4, padx=4)
        self.ttk.Button(consulta, text="Ver día del Sol", command=self.filtrar_dia).grid(row=1, column=5, padx=4)
        self.ttk.Button(consulta, text="Quitar filtro", command=self.quitar_filtro).grid(row=1, column=6, padx=4)
        self.filtro_info = self.tk.StringVar(value="Sin filtro. Las horas filtran muestras locales; inicio incluido y fin excluido.")
        self.ttk.Label(consulta, textvariable=self.filtro_info, wraplength=950).grid(row=2, column=0, columnspan=7, sticky="w", padx=4, pady=(4, 0))
        self.vistas = self.ttk.Notebook(derecha)
        self.vistas.pack(fill="both", expand=True)
        self.paneles, self.figuras, self.canvases, self.barras = {}, {}, {}, {}
        for nombre in ["Planta y 3D", "Triángulo y vértices", "Mapa solar", "Radiación y pérdidas", "Resultados"]:
            marco = self.ttk.Frame(self.vistas)
            self.vistas.add(marco, text=nombre)
            self.paneles[nombre] = marco
        for nombre in ["Planta y 3D", "Triángulo y vértices", "Mapa solar", "Radiación y pérdidas"]:
            fig = Figure(figsize=(11, 7), constrained_layout=True)
            marco = self.paneles[nombre]
            canvas = self.Canvas(fig, master=marco)
            barra = self.Toolbar(canvas, marco, pack_toolbar=False)
            barra.update()
            barra.pack(side="bottom", fill="x")
            if nombre == "Planta y 3D":
                controles = self.ttk.Frame(marco, padding=4)
                controles.pack(fill="x")
                self.ttk.Label(controles, text="Vista 3D:").pack(side="left")
                self.vista_3d = self.tk.StringVar(value="Isométrica")
                selector = self.ttk.Combobox(controles, textvariable=self.vista_3d, state="readonly", width=18,
                                              values=["Isométrica", "Frontal (sur)", "Posterior (norte)", "Lateral este", "Lateral oeste", "Superior"])
                selector.pack(side="left", padx=4)
                selector.bind("<<ComboboxSelected>>", self.cambiar_vista_3d)
                for texto, de, da in [("◀", 0, -5), ("▶", 0, 5), ("▲", 5, 0), ("▼", -5, 0)]:
                    self.ttk.Button(controles, text=texto, width=3, command=lambda e=de, a=da: self.girar_camara(e, a)).pack(side="left", padx=2)
            if nombre == "Triángulo y vértices":
                tabla_marco = self.ttk.Frame(marco)
                tabla_marco.pack(side="bottom", fill="x")
                campos = ["vertex","east_m","north_m","height_m","horizontal_distance_m","delta_height_m","azimuth","elevation"]
                self.tabla = self.ttk.Treeview(tabla_marco, columns=campos, show="headings", height=4)
                for k, texto in zip(campos, ["Vértice","Este [m]","Norte [m]","Altura [m]","d horizontal [m]","Δaltura [m]","Azimut [°]","Elevación [°]"]):
                    self.tabla.heading(k, text=texto)
                    self.tabla.column(k, width=100, minwidth=65)
                horizontal = self.ttk.Scrollbar(tabla_marco, orient="horizontal", command=self.tabla.xview)
                self.tabla.configure(xscrollcommand=horizontal.set)
                horizontal.pack(side="bottom", fill="x")
                self.tabla.pack(fill="x")
                self.tabla.bind("<<TreeviewSelect>>", self._seleccionar_vertice)
            canvas.get_tk_widget().pack(fill="both", expand=True)
            self.figuras[nombre], self.canvases[nombre], self.barras[nombre] = fig, canvas, barra
        self.canvases["Mapa solar"].mpl_connect("button_press_event", self._pulsar_mapa)
        marco = self.paneles["Resultados"]
        self.texto = self.tk.Text(marco, wrap="word", font=("TkDefaultFont", 11), padx=12, pady=10)
        scroll = self.ttk.Scrollbar(marco, command=self.texto.yview)
        self.texto.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.texto.pack(fill="both", expand=True)

    def _formulario(self, padre, campos, variables, edificio=False):
        marco = self.ttk.Frame(padre)
        marco.pack(fill="x", pady=5)
        for i, campo in enumerate(campos):
            key, nombre, *opciones = campo
            self.ttk.Label(marco, text=nombre).grid(row=i, column=0, sticky="w", padx=(0, 6), pady=4)
            var = self.tk.StringVar()
            variables[key] = var
            if opciones:
                entrada = self.ttk.Combobox(marco, textvariable=var, values=opciones[0], state="readonly", width=17)
            else:
                entrada = self.ttk.Entry(marco, textvariable=var, width=19)
            entrada.grid(row=i, column=1, sticky="ew", pady=4)
            var.trace_add("write", self._programar_edicion)
        marco.columnconfigure(1, weight=1)

    def _programar_edicion(self, *args):
        if self._loading:
            return
        if self._pending is not None:
            self.raiz.after_cancel(self._pending)
        self._pending = self.raiz.after(450, self.actualizar_edicion)

    def _cargar_campos(self):
        self._loading = True
        for k, v in self.variables.items():
            v.set(hora_a_texto(self.config["sun_hour"]) if k == "sun_time" else str(self.config[k]))
        self._refrescar_lista()
        self._cargar_edificio()
        self._loading = False

    def _refrescar_lista(self):
        ids = {b["id"] for b in self.config["buildings"]}
        for key in self.lista.get_children():
            if key not in ids:
                self.lista.delete(key)
        for b in self.config["buildings"]:
            valores = (b["name"], f"{b['height']:g}")
            if self.lista.exists(b["id"]):
                self.lista.item(b["id"], values=valores)
            else:
                self.lista.insert("", "end", iid=b["id"], values=valores)
        if self.seleccion in ids:
            self.lista.selection_set(self.seleccion)

    def _cargar_edificio(self):
        previo = self._loading
        self._loading = True
        b = next((b for b in self.config["buildings"] if b["id"] == self.seleccion), None)
        for k, v in self.edificio_vars.items():
            v.set(str(b[k]) if b else "")
        self._loading = previo

    def leer_config(self, incluir_edificio=True):
        c = copy.deepcopy(self.config)
        for k, var in self.variables.items():
            if k == "sun_time":
                c["sun_hour"] = hora_a_decimal(var.get())
            else:
                c[k] = var.get().strip()
        if c["radiation_source"] == "NASA POWER":
            c["minutes"] = 60
            if self.variables["minutes"].get() != "60":
                previo, self._loading = self._loading, True
                self.variables["minutes"].set("60")
                self._loading = previo
        if incluir_edificio and self.seleccion:
            b = next((b for b in c["buildings"] if b["id"] == self.seleccion), None)
            if b is not None:
                for k, var in self.edificio_vars.items():
                    b[k] = var.get().strip()
        return validar_proyecto(c)

    @staticmethod
    def firma_solar(c):
        return tuple(c[k] for k in ("latitude","longitude","altitude","timezone","start","end","minutes", "radiation_source", "weather_key"))

    def actualizar_edicion(self):
        self._pending = None
        try:
            c = self.leer_config()
            posiciones = self.posiciones
            if c["solar_mode"] == "pvlib" and self.solar_signature != self.firma_solar(c):
                posiciones = None
            modelo = analizar_geometria(c, posiciones)
            self.config = c
            self.posiciones = posiciones
            self.aviso.set("")
            self._refrescar_lista()
            self._actualizar_vistas(modelo)
            if c["solar_mode"] == "pvlib" and self.posiciones is None:
                self.estado.set("Geometría actualizada. Pulse Calcular trayectorias y radiación para este recurso y periodo.")
            else:
                self.estado.set("Vistas actualizadas. Seleccione un vértice para revisar su cálculo.")
        except Exception as error:
            self.aviso.set(str(error))
            self.estado.set(f"Complete los datos: {error} Las vistas conservan el último caso válido.")

    def _actualizar_vistas(self, modelo=None):
        modelo = analizar_geometria(self.config, self.posiciones) if modelo is None else modelo
        self.modelo = modelo
        if self.filtro and (not self.config["start"] <= self.filtro["start"] <= self.filtro["end"] <= self.config["end"]
                            or "timestamp" not in modelo["solar"] or (len(modelo["solar"]) and modelo["solar"]["timestamp"].isna().any())):
            self.quitar_filtro(actualizar=False)
        self.modelo_vista = filtrar_modelo(modelo, self.filtro)
        vista = (self.ax3d.elev, self.ax3d.azim) if hasattr(self, "ax3d") else None
        _, self.ax_planta, self.ax3d = dibujar_geometria(modelo, self.seleccion, self.vertice.get(),
                                                        self.figuras["Planta y 3D"], vista)
        figura_triangulo(modelo, self.seleccion, self.vertice.get(), self.figuras["Triángulo y vértices"])
        figura_mapa_modelo(self.modelo_vista, self.seleccion, self.vertice.get(), self.figuras["Mapa solar"])
        figura_radiacion(self.modelo_vista, self.figuras["Radiación y pérdidas"])
        self._anotacion = None
        for canvas in self.canvases.values():
            canvas.draw_idle()
        self.tabla.delete(*self.tabla.get_children())
        filas = modelo["vertices"].loc[modelo["vertices"]["building_id"] == self.seleccion]
        for _, fila in filas.iterrows():
            self.tabla.insert("", "end", iid=fila["vertex"],
                              values=[fila["vertex"]]+[f"{fila[k]:.3f}" for k in self.tabla["columns"][1:]])
        self.texto.configure(state="normal")
        self.texto.delete("1.0", "end")
        self.texto.insert("1.0", resumen_modelo(self.modelo_vista))
        self.texto.configure(state="disabled")

    def _seleccionar_lista(self, evento=None):
        if self._loading:
            return
        elegidos = self.lista.selection()
        if elegidos and elegidos[0] != self.seleccion:
            self.seleccion = elegidos[0]
            self._cargar_edificio()
            self._actualizar_vistas()

    def _seleccionar_vertice(self, evento=None):
        seleccion = self.tabla.selection()
        if seleccion and seleccion[0] != self.vertice.get():
            self.vertice.set(seleccion[0])

    def cambiar_vista_3d(self, evento=None):
        vistas = {"Isométrica": (28, -55), "Frontal (sur)": (0, -90), "Posterior (norte)": (0, 90),
                  "Lateral este": (0, 0), "Lateral oeste": (0, 180), "Superior": (90, -90)}
        self.ax3d.view_init(*vistas[self.vista_3d.get()])
        self.canvases["Planta y 3D"].draw_idle()

    def girar_camara(self, elevacion, azimut):
        self.ax3d.view_init(elev=np.clip(self.ax3d.elev+elevacion, -90, 90), azim=(self.ax3d.azim+azimut+180) % 360-180)
        self.vista_3d.set("Personalizada")
        self.canvases["Planta y 3D"].draw_idle()

    def aplicar_filtro(self):
        try:
            filtro = validar_filtro({k: v.get().strip() for k, v in self.filtro_vars.items()}, self.config)
            filtrar_modelo(self.modelo, filtro)
            self.filtro = filtro
            self.filtro_info.set(f"{filtro['start']} a {filtro['end']} · {filtro['from_time']}–{filtro['to_time']} cada día. Muestras locales; fin excluido.")
            self._actualizar_vistas(self.modelo)
        except Exception as error:
            self.mb.showerror("Filtro de resultados", str(error), parent=self.raiz)

    def filtrar_dia(self):
        try:
            self.actualizar_edicion()
            if self.aviso.get():
                raise ValueError(self.aviso.get())
            for key in ("start", "end"):
                self.filtro_vars[key].set(self.config["sun_date"])
            self.filtro_vars["from_time"].set("00:00")
            self.filtro_vars["to_time"].set("24:00")
            self.aplicar_filtro()
        except Exception as error:
            self.mb.showerror("Filtro de resultados", str(error), parent=self.raiz)

    def quitar_filtro(self, actualizar=True):
        self.filtro = None
        for key in ("start", "end"):
            self.filtro_vars[key].set(self.config[key])
        self.filtro_vars["from_time"].set("00:00")
        self.filtro_vars["to_time"].set("24:00")
        self.filtro_info.set("Sin filtro. Las horas filtran muestras locales; inicio incluido y fin excluido.")
        if actualizar:
            self._actualizar_vistas(self.modelo)

    def _pulsar_mapa(self, evento):
        if evento.xdata is None or evento.inaxes is None or self.modelo is None or self.barras["Mapa solar"].mode:
            return
        d = self.modelo_vista["solar"]
        if not len(d):
            return
        dist = (d["azimuth"]-evento.xdata)**2+(d["apparent_elevation"]-evento.ydata)**2
        q = d.loc[dist.idxmin()]
        if dist.min() > 16:
            return
        if self._anotacion is not None:
            self._anotacion.remove()
        fecha = str(q["timestamp"]) if "timestamp" in q else "Sin fecha"
        texto = (f"{fecha}\nAzimut {q['azimuth']:.2f}° · elevación {q['apparent_elevation']:.2f}°\n"
                 f"AOI {q['aoi']:.2f}° · {'Bloqueado' if q['sombreado'] else 'Visible'}\n"
                 f"{'Delante' if q['in_front'] else 'Detrás'} del plano")
        if "poa_shaded" in q:
            texto += f"\nPOA recibida {q['poa_shaded']:.2f} W/m² · pérdida {q['poa_loss']:.2f} W/m²"
            if np.isfinite(q['poa_loss_percent']):
                texto += f" ({q['poa_loss_percent']:.2f} %)"
        self._anotacion = evento.inaxes.annotate(texto, (q["azimuth"], q["apparent_elevation"]),
                                                xytext=(12, 10), textcoords="offset points", fontsize=9,
                                                bbox={"boxstyle": "round", "fc": "white", "alpha": .95})
        self.canvases["Mapa solar"].draw_idle()

    def agregar(self):
        try:
            self.config = self.leer_config()
            ids = {b["id"] for b in self.config["buildings"]}
            i = 1
            while f"B{i}" in ids:
                i += 1
            x = max([self.config["receiver_x"]]+[b["x"]+np.hypot(b["width"], b["depth"])/2 for b in self.config["buildings"]])+30
            self.seleccion = f"B{i}"
            self.config["buildings"].append({"id": self.seleccion, "name": f"Edificio {i}", "x": x,
                                             "y": self.config["receiver_y"], "width": 20., "depth": 20.,
                                             "height": 10., "rotation": 0.})
            self._cargar_campos()
            self._actualizar_vistas()
            self.entradas.select(1)
        except Exception as error:
            self.mb.showerror("Edificio", str(error), parent=self.raiz)

    def duplicar(self):
        if not self.seleccion:
            return
        try:
            original = next(b for b in self.leer_config()["buildings"] if b["id"] == self.seleccion)
            self.agregar()
            nuevo_id = self.seleccion
            original = copy.deepcopy(original)
            original.update(id=nuevo_id, name=original["name"]+" copia",
                            x=original["x"]+np.hypot(original["width"], original["depth"])+10)
            self.config["buildings"][-1] = original
            self._cargar_campos()
            self._actualizar_vistas()
        except Exception as error:
            self.mb.showerror("Duplicar", str(error), parent=self.raiz)

    def quitar(self):
        if self.seleccion is None:
            return
        self.config["buildings"] = [b for b in self.config["buildings"] if b["id"] != self.seleccion]
        self.seleccion = self.config["buildings"][0]["id"] if self.config["buildings"] else None
        self._cargar_campos()
        self._actualizar_vistas()

    def descripcion_recurso(self):
        if self.config["radiation_source"] == "NASA POWER" and self.config.get("weather_data"):
            meta = self.config.get("nasa_metadata", {})
            return (f"NASA POWER: {len(self.config['weather_data'])} intervalos horarios incluidos en el proyecto.\n"
                    f"Datos originales y CSV: {meta.get('cache_folder', 'incluidos en el proyecto')}.")
        return "Tabla meteorológica incluida en el proyecto." if self.config["weather_data"] else "Sin tabla meteorológica importada."

    def descargar_nasa(self):
        if self.trabajando or self.nasa_trabajando:
            self.estado.set("Espere a que termine el cálculo o la descarga actual.")
            return
        try:
            c = self.leer_config()
            # La validación temporal se realiza antes de iniciar la petición remota.
            hoy = pd.Timestamp.now(tz=c["timezone"]).strftime("%Y-%m-%d")
            if c["end"] >= hoy:
                raise ValueError("NASA POWER requiere un periodo histórico completo. Cambie las fechas del sitio; por ejemplo, use 2025-01-01 a 2025-12-31.")
        except Exception as error:
            self.mb.showerror("NASA POWER", str(error), parent=self.raiz)
            return
        firma, revision = self.firma_solar(c), self.revision_proyecto
        self.nasa_trabajando, self.nasa_pendiente = True, None
        self.nasa_progreso = "Descargando NASA POWER; la ventana permanece disponible…"
        self.boton_nasa.configure(state="disabled")
        self.progreso_nasa.pack(fill="x", after=self.boton_nasa, pady=3)
        self.progreso_nasa.start(12)
        def progreso(texto):
            self.nasa_progreso = texto
        def trabajo():
            try:
                preparado = descargar_nasa_power(c, progreso=progreso)
                progreso("NASA POWER: calculando posiciones y preparando el recurso horario…")
                posiciones = calcular_posiciones(preparado)
                self.nasa_pendiente = (True, (preparado, posiciones), firma, revision)
            except Exception as error:
                self.nasa_pendiente = (False, str(error), firma, revision)
        threading.Thread(target=trabajo, daemon=True).start()
        self.raiz.after(100, self._revisar_nasa)

    def _revisar_nasa(self):
        if self.nasa_pendiente is None:
            self.estado.set(self.nasa_progreso)
            self.raiz.after(100, self._revisar_nasa)
            return
        self.nasa_trabajando = False
        self.progreso_nasa.stop()
        self.progreso_nasa.pack_forget()
        self.boton_nasa.configure(state="normal")
        ok, datos, firma, revision = self.nasa_pendiente
        if not ok:
            self.estado.set("No se pudo cargar NASA POWER; se conserva el último caso calculado.")
            self.mb.showerror("NASA POWER", datos, parent=self.raiz)
            return
        try:
            actual = self.leer_config()
            if revision != self.revision_proyecto or firma != self.firma_solar(actual):
                self.estado.set("La descarga quedó guardada. El sitio o recurso cambió durante la descarga; pulse Descargar y usar NASA POWER nuevamente.")
                return
            preparado, posiciones = datos
            for k in ("minutes", "radiation_source", "solar_mode", "weather_data", "weather_key", "nasa_metadata"):
                actual[k] = preparado[k]
            self.config = validar_proyecto_interfaz(actual)
            self.posiciones, self.solar_signature = posiciones, self.firma_solar(self.config)
            self._cargar_campos()
            self.weather_info.set(self.descripcion_recurso())
            self._actualizar_vistas()
            self.estado.set("NASA POWER cargado. Intervalo: 60 minutos. Los datos están incluidos en el proyecto y pueden guardarse para trabajar sin conexión.")
        except Exception as error:
            self.estado.set("La descarga quedó guardada, pero no pudo aplicarse al proyecto.")
            self.mb.showerror("NASA POWER", str(error), parent=self.raiz)

    def calcular_solar(self):
        if self.trabajando or self.nasa_trabajando:
            self.estado.set("Hay un cálculo o descarga en curso; espere a que termine.")
            return
        try:
            c = self.leer_config()
            c["solar_mode"] = "pvlib"
            self.config = c
        except Exception as error:
            self.mb.showerror("Datos del sitio", str(error), parent=self.raiz)
            return
        self.trabajando, self.pendiente = True, None
        self.posiciones, self.solar_signature = None, None
        self._actualizar_vistas()
        firma = self.firma_solar(c)
        self.estado.set("Calculando trayectorias y recurso solar…")
        def trabajo():
            try:
                self.pendiente = (True, calcular_posiciones(c), firma)
            except Exception as error:
                self.pendiente = (False, str(error), firma)
        threading.Thread(target=trabajo, daemon=True).start()
        self.raiz.after(100, self._revisar_calculo)

    def _revisar_calculo(self):
        if self.pendiente is None:
            self.raiz.after(100, self._revisar_calculo)
            return
        self.trabajando = False
        ok, datos, firma = self.pendiente
        if not ok:
            self.estado.set("No se pudo calcular la trayectoria.")
            self.mb.showerror("Posición solar", datos, parent=self.raiz)
            return
        try:
            c = self.leer_config()
            if firma != self.firma_solar(c) or c["solar_mode"] != "pvlib":
                self.estado.set("El sitio cambió durante el cálculo. Pulse Calcular trayectorias nuevamente.")
                return
            self.config = c
            self.posiciones, self.solar_signature = datos, firma
            self._actualizar_vistas()
            self.estado.set("Recurso solar calculado. Edite edificios u orientación para comparar la radiación recibida y las pérdidas.")
        except Exception as error:
            self.aviso.set(str(error))

    def _usar_proyecto(self, c):
        c = validar_proyecto_interfaz(c)
        if c["radiation_source"] != "Cielo despejado":
            tabla_meteorologica(c)
        posiciones = None
        if c["solar_mode"] == "Archivo":
            posiciones = preparar_importacion("Posiciones solares", pd.DataFrame(c["imported_solar"]), c)
        analizar_geometria(c, posiciones)
        self.config = c
        self.revision_proyecto += 1
        self.seleccion = self.config["buildings"][0]["id"] if self.config["buildings"] else None
        self.posiciones, self.solar_signature = posiciones, None
        self.quitar_filtro(actualizar=False)
        self._cargar_campos()
        self.weather_info.set(self.descripcion_recurso())
        self._actualizar_vistas()
        self.aviso.set("")
        if self.config["solar_mode"] == "pvlib":
            if self.trabajando:
                self.estado.set("Proyecto abierto. Espere el cálculo anterior y pulse Calcular trayectorias.")
            else:
                self.calcular_solar()

    def cargar_demo(self):
        self._usar_proyecto(proyecto_ejemplo())
        self.estado.set("Ejemplo cargado. Los edificios y la orientación del receptor son editables.")

    def nuevo(self):
        self._usar_proyecto(proyecto_nuevo())

    def abrir(self):
        ruta = self.fd.askopenfilename(parent=self.raiz, title="Abrir proyecto",
                                       initialdir=str(self.carpeta_proyectos),
                                       filetypes=[("Proyecto JSON", "*.json")])
        if ruta:
            try:
                self._usar_proyecto(json.loads(Path(ruta).read_text(encoding="utf-8")))
                self.carpeta_proyectos = Path(ruta).parent
            except Exception as error:
                self.mb.showerror("Abrir proyecto", str(error), parent=self.raiz)

    def guardar(self):
        try:
            c = self.leer_config()
            analizar_geometria(c, self.posiciones)
            ruta = self.fd.asksaveasfilename(parent=self.raiz, defaultextension=".json",
                                             initialfile="proyecto_sombras.json", filetypes=[("Proyecto JSON", "*.json")])
            if ruta:
                Path(ruta).write_text(json.dumps(c, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
                self.estado.set("Proyecto guardado con sitio, edificios, orientación y datos importados.")
        except Exception as error:
            self.mb.showerror("Guardar proyecto", str(error), parent=self.raiz)

    def exportar(self):
        self.actualizar_edicion()
        if self.aviso.get():
            self.mb.showinfo("Exportación", "Complete los datos válidos antes de exportar.", parent=self.raiz)
            return
        if self.posiciones is None:
            self.mb.showinfo("Exportación", "Calcule las trayectorias o importe posiciones antes de exportar todo.", parent=self.raiz)
            return
        ruta = self.fd.askdirectory(parent=self.raiz, title="Carpeta de resultados; use una distinta para cada escenario")
        if ruta:
            try:
                exportar_modelo(self.modelo, ruta, self.seleccion, self.vertice.get())
                if self.filtro:
                    exportar_consulta(self.modelo_vista, ruta)
                self.estado.set(f"Proyecto, radiación, pérdidas, tablas e imágenes exportados a {ruta}")
            except Exception as error:
                self.mb.showerror("Exportación", str(error), parent=self.raiz)

    def abrir_enlace(self, url):
        try:
            if not webbrowser.open(url):
                self.mb.showinfo("Enlace", f"Abra esta dirección en su navegador:\n{url}", parent=self.raiz)
        except Exception as error:
            self.mb.showerror("Enlace", f"No fue posible abrir el navegador.\n{url}\n{error}", parent=self.raiz)

    def guardar_imagen(self):
        nombre = self.vistas.tab(self.vistas.select(), "text")
        fig = self.figuras.get(nombre, self.figuras["Mapa solar"])
        ruta = self.fd.asksaveasfilename(parent=self.raiz, title="Guardar vista actual", defaultextension=".png",
                                         filetypes=[("PNG", "*.png"), ("PDF", "*.pdf")])
        if ruta:
            try:
                fig.savefig(ruta, dpi=170, bbox_inches="tight")
                self.estado.set(f"Vista guardada: {ruta}")
            except Exception as error:
                self.mb.showerror("Guardar vista", str(error), parent=self.raiz)

    def guardar_plantilla_radiacion(self):
        try:
            c = self.leer_config()
            ruta = self.fd.asksaveasfilename(parent=self.raiz, defaultextension=".csv",
                    initialfile="ejemplo_radiacion_cielo_despejado.csv",
                    filetypes=[("CSV", "*.csv"), ("Excel", "*.xlsx")])
            if not ruta:
                return
            self.estado.set("Generando ejemplo de formato con radiación estimada de cielo despejado…")
            self.raiz.update_idletasks()
            datos = plantilla_meteorologica(c)
            if Path(ruta).suffix.lower() == ".xlsx":
                datos.to_excel(ruta, index=False)
            else:
                datos.to_csv(ruta, index=False)
            self.weather_info.set(f"Ejemplo de formato guardado: {Path(ruta).name}. Contiene cielo despejado estimado.")
            self.estado.set("Ejemplo guardado. Para un estudio meteorológico, sustituya esos valores por los datos del sitio.")
        except Exception as error:
            self.mb.showerror("Formato de radiación", str(error), parent=self.raiz)

    def importar_radiacion(self):
        ruta = self.fd.askopenfilename(parent=self.raiz, title="Importar radiación: timestamp, ghi; dni y dhi opcionales",
                                       filetypes=[("CSV o Excel", "*.csv *.xlsx *.xlsm")])
        if not ruta:
            return
        try:
            c = self.leer_config()
            datos = leer_tabla(Path(ruta))
            serial = datos.to_json(orient="records", date_format="iso", force_ascii=False)
            c["weather_data"] = json.loads(serial)
            c["weather_key"] = hashlib.sha256(serial.encode("utf-8")).hexdigest()
            c["radiation_source"] = "Datos meteorológicos"
            c["nasa_metadata"] = {}
            tabla = tabla_meteorologica(c)
            # Conserva únicamente fechas y componentes que intervienen en el cálculo.
            limpia = tabla.copy()
            limpia.insert(0, "timestamp", limpia.index.map(lambda t: t.isoformat()))
            c["weather_data"] = limpia.to_dict("records")
            datos_solares = calcular_posiciones(c)
        except Exception as error:
            self.mb.showerror("Radiación: revise la tabla", str(error), parent=self.raiz)
            return
        ventana = self.tk.Toplevel(self.raiz)
        ventana.title("Radiación: vista previa")
        ventana.geometry("900x470")
        ventana.transient(self.raiz)
        ventana.grab_set()
        self.ttk.Label(ventana, wraplength=850,
                      text=f"{len(tabla)} intervalos. Irradiancias en W/m²; fechas al inicio de cada intervalo. Se comprobó cobertura del periodo.").pack(padx=12, pady=10)
        texto = self.tk.Text(ventana, wrap="none", font=("TkFixedFont", 10))
        texto.pack(fill="both", expand=True, padx=12, pady=8)
        texto.insert("1.0", limpia.head(15).to_string(index=False))
        texto.configure(state="disabled")
        def aceptar():
            try:
                self.config = c
                self.posiciones = datos_solares
                self.solar_signature = self.firma_solar(c)
                self._cargar_campos()
                self._actualizar_vistas()
                self.weather_info.set(f"{Path(ruta).name}: {len(tabla)} intervalos incluidos en el proyecto.")
                self.estado.set("Recurso importado. La irradiancia y las pérdidas se calculan con estos datos.")
                ventana.destroy()
            except Exception as error:
                self.mb.showerror("Radiación", str(error), parent=ventana)
        self.ttk.Button(ventana, text="Usar estos datos", command=aceptar).pack(pady=10)

    def ejecutar(self):
        self.raiz.mainloop()


def descripcion_formato(tipo):
    return {
        "Edificios": "Centro x al este, y al norte [m]. Ancho, fondo y altura [m]. Giro horario [°]; 0° = ancho E-O, fondo N-S. Base z=0. Sustituye la lista de edificios.",
        "Máscara angular": "azimuth y obstruction_elevation en grados. obstacle_id agrupa cada obstáculo; puntos del mismo grupo se interpolan. No reconstruye edificios 3D. Sustituye la máscara geométrica.",
        "Posiciones solares": "azimuth y apparent_elevation en grados; timestamp ISO opcional. Sin zona horaria se interpreta la del sitio. Se conservan solo posiciones sobre el horizonte. No se asume muestreo regular.",
    }[tipo]


def plantilla_datos(tipo, c):
    if tipo == "Edificios":
        return pd.DataFrame(c["buildings"] or proyecto_ejemplo()["buildings"]).drop(columns="id", errors="ignore")
    if tipo == "Máscara angular":
        return pd.DataFrame({"azimuth": [80., 90., 100., 250., 270., 290.],
                             "obstruction_elevation": [10., 18., 10., 12., 20., 12.],
                             "obstacle_id": ["M1"]*3+["M2"]*3})
    return pd.DataFrame({"timestamp": [f"{c['start']}T09:00:00", f"{c['start']}T12:00:00", f"{c['start']}T15:00:00"],
                         "azimuth": [120., 170., 225.], "apparent_elevation": [20., 45., 30.]})


def preparar_importacion(tipo, tabla, c):
    tabla = tabla.copy()
    tabla.columns = [nombre_normalizado(k) for k in tabla.columns]
    if tabla.empty:
        raise ValueError("El archivo no tiene filas de datos.")
    if tipo == "Edificios":
        columnas = ["name","x","y","width","depth","height","rotation"]
        for k in columnas:
            if k not in tabla:
                raise ValueError(f"Falta la columna {k}. Use la plantilla Edificios.")
        edificios = []
        for i, (_, fila) in enumerate(tabla.iterrows(), 2):
            b = {k: fila[k] for k in columnas}
            b["id"] = f"B{i-1}"
            prueba = copy.deepcopy(c)
            prueba["buildings"] = [b]
            try:
                edificios.append(validar_proyecto(prueba)["buildings"][0])
            except Exception as error:
                raise ValueError(f"Fila {i}: {error}") from None
        return pd.DataFrame(edificios)
    ca = buscar_columna(tabla, ("azimuth","azimut","solar_azimuth","gamma","gamma_s"), "azimut")
    opciones = (("obstruction_elevation","elevacion_obstruccion","elevacion_obstaculo","horizon_elevation","elevation","elevacion")
                if tipo == "Máscara angular" else ("apparent_elevation","elevation","elevacion_aparente","elevacion"))
    ce = buscar_columna(tabla, opciones, "elevación")
    for col in (ca, ce):
        valores = pd.to_numeric(tabla[col], errors="coerce")
        malos = ~np.isfinite(valores)
        if malos.any():
            fila = int(np.flatnonzero(malos.to_numpy())[0])+2
            raise ValueError(f"Fila {fila}, columna {col}: falta un número finito.")
    elev = pd.to_numeric(tabla[ce])
    fuera = ~elev.between(0 if tipo == "Máscara angular" else -90, 90)
    if fuera.any():
        fila = int(np.flatnonzero(fuera.to_numpy())[0])+2
        raise ValueError(f"Fila {fila}, columna {ce}: elevación fuera del rango permitido.")
    if tipo == "Máscara angular":
        resultado = preparar_mascara(tabla)
        if "obstacle_id" in resultado and (resultado["obstacle_id"].eq("nan") | resultado["obstacle_id"].str.strip().eq("")).any():
            raise ValueError("Cada punto debe tener obstacle_id cuando esa columna está presente.")
        return resultado.reset_index(drop=True)
    columna_fecha = next((k for k in ("timestamp","datetime","time","fecha_hora","fecha") if k in tabla), None)
    if columna_fecha:
        fechas = []
        for i, valor in enumerate(tabla[columna_fecha], 2):
            try:
                t = pd.Timestamp(valor)
                if pd.isna(t):
                    raise ValueError()
                t = t.tz_localize(c["timezone"]) if t.tzinfo is None else t.tz_convert(c["timezone"])
                fechas.append(t)
            except Exception:
                raise ValueError(f"Fila {i}, columna {columna_fecha}: fecha o zona horaria inválida.") from None
        tabla[columna_fecha] = pd.DatetimeIndex(fechas)
    return preparar_posiciones_solares(tabla).reset_index(drop=True)


def ejecutar_sin_interfaz(argumentos):
    c = validar_proyecto(json.loads(argumentos.proyecto.read_text(encoding="utf-8"))) if argumentos.proyecto else proyecto_ejemplo()
    if argumentos.mascara is not None or argumentos.solar is not None:
        if argumentos.mascara is None or argumentos.solar is None:
            raise SystemExit("Para archivos angulares incluya --mascara y --solar juntos.")
        mascara = preparar_importacion("Máscara angular", leer_tabla(argumentos.mascara), c)
        posiciones = preparar_importacion("Posiciones solares", leer_tabla(argumentos.solar), c)
        serial = posiciones.copy()
        if "timestamp" in serial:
            serial["timestamp"] = serial["timestamp"].map(lambda t: t.isoformat())
        c.update(buildings=[], mask_mode="Archivo", solar_mode="Archivo",
                 imported_mask=mascara.to_dict("records"), imported_solar=serial.to_dict("records"),
                 example_note="Archivos angulares importados; no contienen geometría para reconstruir edificios.")
    else:
        posiciones = (preparar_importacion("Posiciones solares", pd.DataFrame(c["imported_solar"]), c)
                      if c["solar_mode"] == "Archivo" else calcular_posiciones(c))
    modelo = analizar_geometria(c, posiciones)
    seleccionado = c["buildings"][0]["id"] if c["buildings"] else None
    figura = figura_mapa_modelo(modelo, seleccionado)
    print(resumen_modelo(modelo))
    if argumentos.exportar:
        exportar_modelo(modelo, argumentos.exportar, seleccionado)
    if argumentos.salida:
        argumentos.salida.parent.mkdir(parents=True, exist_ok=True)
        figura.savefig(argumentos.salida, dpi=170)
        print(f"Mapa guardado en {argumentos.salida}")


def main():
    argumentos = leer_argumentos()
    if argumentos.no_interfaz or argumentos.salida is not None or argumentos.exportar is not None:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        ejecutar_sin_interfaz(argumentos)
    else:
        c = (json.loads(argumentos.proyecto.read_text(encoding="utf-8")) if argumentos.proyecto
             else proyecto_ejemplo() if argumentos.demo else None)
        aplicacion = AplicacionSombras(c)
        aplicacion.ejecutar()


if __name__ == "__main__":
    main()
