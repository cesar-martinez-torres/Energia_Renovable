"""Comprueba que el ambiente de Python del curso funciona correctamente."""

import sys

import matplotlib
import numpy as np
import numpy_financial as npf
import openpyxl
import pandas as pd
import pvlib
import requests
import scipy


def main() -> None:
    """Ejecuta pruebas breves que no requieren conexion a internet."""
    if sys.version_info[:2] != (3, 13):
        raise RuntimeError("Este curso requiere Python 3.13.")

    capacidad_mw = 363.0
    energia_anual_mwh = 972_915.0
    factor_capacidad = energia_anual_mwh / (capacidad_mw * 8_760)
    if not np.isclose(factor_capacidad, 0.30596, atol=1e-5):
        raise RuntimeError("La prueba numerica no produjo el resultado esperado.")

    ubicacion = pvlib.location.Location(
        latitude=19.054,
        longitude=-98.283,
        tz="America/Mexico_City",
        altitude=2_130,
    )
    fecha = pd.DatetimeIndex(["2026-03-21 12:00"], tz=ubicacion.tz)
    posicion_solar = ubicacion.get_solarposition(fecha)
    if posicion_solar[["apparent_zenith", "azimuth"]].isna().any().any():
        raise RuntimeError("La prueba de posicion solar no produjo datos validos.")

    print(f"Python: {sys.version.split()[0]}")
    print(f"NumPy: {np.__version__}")
    print(f"Pandas: {pd.__version__}")
    print(f"pvlib: {pvlib.__version__}")
    print(f"Factor de capacidad de prueba: {factor_capacidad:.2%}")
    print("Entorno listo para el curso.")


if __name__ == "__main__":
    main()

