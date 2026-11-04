"""Genera un mapa solar polar, cartesiano o combinado con pvlib."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

import matplotlib
import numpy as np
import pandas as pd
from pvlib.location import Location

if "--no-mostrar" in sys.argv:
    matplotlib.use("Agg")

import matplotlib.pyplot as plt


FECHAS_REPRESENTATIVAS = {
    "Equinoccio de marzo": (3, 20),
    "Solsticio de junio": (6, 21),
    "Equinoccio de septiembre": (9, 22),
    "Solsticio de diciembre": (12, 21),
}


def leer_argumentos() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Genera un mapa de trayectoria solar mediante pvlib."
    )
    parser.add_argument(
        "--tipo",
        choices=("polar", "cartesiano", "ambos"),
        default="ambos",
        help="Representación que se generará. Valor predeterminado: ambos.",
    )
    parser.add_argument("--latitud", type=float, default=19.04)
    parser.add_argument("--longitud", type=float, default=-98.20)
    parser.add_argument("--altitud", type=float, default=2160.0)
    parser.add_argument(
        "--zona-horaria",
        default="America/Mexico_City",
        help="Zona horaria reconocida por pandas.",
    )
    parser.add_argument("--anio", type=int, default=2026)
    parser.add_argument(
        "--salida",
        type=Path,
        help="Ruta del archivo PNG. Si se omite, se guarda junto al script.",
    )
    parser.add_argument(
        "--no-mostrar",
        action="store_true",
        help="Guarda la figura sin abrir una ventana.",
    )
    return parser.parse_args()


def calcular_posicion_anual(
    sitio: Location,
    anio: int,
    zona_horaria: str,
) -> pd.DataFrame:
    tiempos = pd.date_range(
        start=f"{anio}-01-01 00:00",
        end=f"{anio + 1}-01-01 00:00",
        freq="1h",
        tz=zona_horaria,
        inclusive="left",
    )
    posicion = sitio.get_solarposition(tiempos)
    return posicion.loc[posicion["apparent_elevation"] > 0].copy()


def calcular_trayectorias_representativas(
    sitio: Location,
    anio: int,
    zona_horaria: str,
) -> dict[str, pd.DataFrame]:
    trayectorias = {}

    for etiqueta, (mes, dia) in FECHAS_REPRESENTATIVAS.items():
        fecha = f"{anio}-{mes:02d}-{dia:02d}"
        tiempos = pd.date_range(
            start=f"{fecha} 00:00",
            end=f"{fecha} 23:55",
            freq="5min",
            tz=zona_horaria,
        )
        posicion = sitio.get_solarposition(tiempos)
        trayectorias[etiqueta] = posicion.loc[
            posicion["apparent_elevation"] > 0
        ].copy()

    return trayectorias


def dibujar_polar(
    ax: plt.Axes,
    posicion_anual: pd.DataFrame,
    trayectorias: dict[str, pd.DataFrame],
):
    puntos = ax.scatter(
        np.radians(posicion_anual["azimuth"]),
        posicion_anual["apparent_zenith"],
        c=posicion_anual.index.dayofyear,
        cmap="twilight_shifted",
        s=5,
        alpha=0.55,
    )

    for etiqueta, trayectoria in trayectorias.items():
        ax.plot(
            np.radians(trayectoria["azimuth"]),
            trayectoria["apparent_zenith"],
            linewidth=2.2,
            label=etiqueta,
        )

    ax.set_theta_zero_location("N")
    ax.set_theta_direction(-1)
    ax.set_rmax(90)
    ax.set_rticks([0, 15, 30, 45, 60, 75, 90])
    ax.set_rlabel_position(225)
    ax.set_title("Mapa solar polar", pad=20)
    ax.legend(loc="lower center", fontsize=8)
    return puntos


def dibujar_cartesiano(
    ax: plt.Axes,
    posicion_anual: pd.DataFrame,
    trayectorias: dict[str, pd.DataFrame],
):
    puntos = ax.scatter(
        posicion_anual["azimuth"],
        posicion_anual["apparent_elevation"],
        c=posicion_anual.index.dayofyear,
        cmap="twilight_shifted",
        s=5,
        alpha=0.55,
    )

    for etiqueta, trayectoria in trayectorias.items():
        elevacion = trayectoria["apparent_elevation"].copy()
        salto_acimut = trayectoria["azimuth"].diff().abs() > 180
        elevacion.loc[salto_acimut] = np.nan
        ax.plot(
            trayectoria["azimuth"],
            elevacion,
            linewidth=2.2,
            label=etiqueta,
        )

    ax.set_xlim(0, 360)
    ax.set_ylim(0, 90)
    ax.set_xticks([0, 45, 90, 135, 180, 225, 270, 315, 360])
    ax.set_xlabel("Acimut solar, azimuth [°]")
    ax.set_ylabel("Elevación aparente, apparent_elevation [°]")
    ax.set_title("Mapa solar cartesiano")
    ax.grid(True, alpha=0.35)
    ax.legend(loc="upper right", fontsize=8)
    return puntos


def crear_figura(
    tipo: str,
    posicion_anual: pd.DataFrame,
    trayectorias: dict[str, pd.DataFrame],
    nombre_sitio: str,
    anio: int,
) -> tuple[plt.Figure, object]:
    if tipo == "polar":
        figura, eje = plt.subplots(
            figsize=(9, 8),
            subplot_kw={"projection": "polar"},
            constrained_layout=True,
        )
        puntos = dibujar_polar(eje, posicion_anual, trayectorias)
        ejes_color = eje
    elif tipo == "cartesiano":
        figura, eje = plt.subplots(figsize=(10, 6), constrained_layout=True)
        puntos = dibujar_cartesiano(eje, posicion_anual, trayectorias)
        ejes_color = eje
    else:
        figura = plt.figure(figsize=(16, 7), constrained_layout=True)
        eje_polar = figura.add_subplot(1, 2, 1, projection="polar")
        eje_cartesiano = figura.add_subplot(1, 2, 2)
        puntos = dibujar_polar(eje_polar, posicion_anual, trayectorias)
        dibujar_cartesiano(eje_cartesiano, posicion_anual, trayectorias)
        ejes_color = [eje_polar, eje_cartesiano]

    figura.suptitle(
        f"Trayectoria solar anual — {nombre_sitio}, {anio}",
        fontsize=15,
        fontweight="bold",
    )
    figura.colorbar(
        puntos,
        ax=ejes_color,
        orientation="horizontal",
        fraction=0.05,
        pad=0.08,
        label="Día del año",
    )
    return figura, ejes_color


def main() -> None:
    argumentos = leer_argumentos()

    sitio = Location(
        latitude=argumentos.latitud,
        longitude=argumentos.longitud,
        tz=argumentos.zona_horaria,
        altitude=argumentos.altitud,
        name="Puebla",
    )

    posicion_anual = calcular_posicion_anual(
        sitio,
        argumentos.anio,
        argumentos.zona_horaria,
    )
    trayectorias = calcular_trayectorias_representativas(
        sitio,
        argumentos.anio,
        argumentos.zona_horaria,
    )

    figura, _ = crear_figura(
        argumentos.tipo,
        posicion_anual,
        trayectorias,
        sitio.name,
        argumentos.anio,
    )

    salida = argumentos.salida
    if salida is None:
        salida = Path(__file__).resolve().parent / (
            f"mapa_solar_{argumentos.tipo}_{argumentos.anio}.png"
        )

    salida = salida.expanduser().resolve()
    salida.parent.mkdir(parents=True, exist_ok=True)
    figura.savefig(salida, dpi=180, bbox_inches="tight")
    print(f"Mapa solar guardado en: {salida}")

    if not argumentos.no_mostrar:
        plt.show()
    else:
        plt.close(figura)


if __name__ == "__main__":
    main()
