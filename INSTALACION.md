# Instalación del entorno del curso

Estas instrucciones preparan en Windows un ambiente independiente para los ejercicios de Energía Renovable. No se requiere experiencia previa con Python.

## 1. Requisitos

Instala los siguientes programas:

1. Git: <https://git-scm.com/download/win>
2. Python 3.13: <https://www.python.org/downloads/>
3. Visual Studio Code: <https://code.visualstudio.com/>

Durante la instalación de Python, activa la opción **Add Python to PATH**. En Visual Studio Code instala las extensiones **Python** y **Jupyter**, ambas publicadas por Microsoft.

## 2. Descargar el repositorio

Abre PowerShell en la carpeta donde guardarás el curso y ejecuta:

```powershell
git clone https://github.com/cesar-martinez-torres/Energia_Renovable.git
cd Energia_Renovable
```

Si ya descargaste el repositorio, abre PowerShell dentro de su carpeta y continúa con el paso siguiente.

## 3. Crear el ambiente virtual

En PowerShell, desde la carpeta principal del repositorio, ejecuta:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\setup.ps1
```

El primer comando permite ejecutar el instalador únicamente en la ventana actual. El segundo crea la carpeta `.venv`, instala las bibliotecas del curso y registra el kernel de Jupyter.

La instalación inicial puede tardar varios minutos. Debe finalizar con el mensaje `Entorno verificado correctamente.`

## 4. Verificar la instalación con Python

En PowerShell ejecuta:

```powershell
.\.venv\Scripts\python.exe .\00_verificar_entorno.py
```

El programa debe terminar con el mensaje `Entorno listo para el curso.`

## 5. Usar Jupyter

Los ejercicios del curso se desarrollan en cuadernos `.ipynb`. En Visual Studio Code abre el cuaderno correspondiente, selecciona **Python (Energia Renovable 2026)** y ejecuta las celdas en orden.

El cuaderno `01_evaluacion_economica_LCOE.ipynb` reproduce el ejercicio de evaluación económica del Parque Solar Villanueva.

También puedes iniciar Jupyter Lab desde PowerShell:

```powershell
.\.venv\Scripts\python.exe -m jupyter lab
```

## Problemas frecuentes

- **No se encontró Python 3.13:** instala esa versión, cierra PowerShell y abre una ventana nueva.
- **No se puede ejecutar `setup.ps1`:** confirma que ejecutaste `Set-ExecutionPolicy -Scope Process Bypass` en la misma ventana.
- **No aparece el kernel:** reinicia Visual Studio Code y vuelve a abrir el cuaderno.
- **La instalación se interrumpió:** vuelve a ejecutar `.\setup.ps1`; el instalador reutiliza y actualiza el ambiente existente.


