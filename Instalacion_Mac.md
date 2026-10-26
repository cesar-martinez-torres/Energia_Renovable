# Instalación del entorno del curso en macOS

Estas instrucciones preparan un ambiente independiente para los ejercicios de Energía Renovable. No se requiere experiencia previa con Python.

El procedimiento funciona tanto en equipos Mac con procesador Apple Silicon como en equipos Mac con procesador Intel. El ambiente `.venv` se crea localmente en cada computadora.

## 1. Abrir Terminal

En Finder, abre **Aplicaciones**, después **Utilidades** y selecciona **Terminal**. También puedes buscar `Terminal` mediante Spotlight.

Todos los comandos de esta guía se escriben en Terminal. Después de escribir cada comando, presiona **Enter**.

## 2. Instalar Git

Comprueba si Git está instalado:

```bash
git --version
```

Si macOS solicita instalar las herramientas para desarrolladores, acepta la instalación. Si no aparece el aviso y Git no está disponible, ejecuta:

```bash
xcode-select --install
```

Cuando termine la instalación, cierra Terminal, abre una ventana nueva y comprueba nuevamente:

```bash
git --version
```

## 3. Instalar Python 3.13

1. Abre [https://www.python.org/downloads/macos/](https://www.python.org/downloads/macos/).
2. Descarga el instalador de Python 3.13 para macOS.
3. Abre el archivo descargado y completa la instalación con las opciones predeterminadas.
4. Cierra Terminal y abre una ventana nueva.

Comprueba la versión instalada:

```bash
python3.13 --version
```

El resultado debe comenzar con `Python 3.13`.

No utilices `sudo pip install`. El instalador del curso creará un ambiente independiente para evitar modificar Python del sistema.

## 4. Instalar Visual Studio Code

1. Descarga Visual Studio Code desde [https://code.visualstudio.com/download](https://code.visualstudio.com/download).
2. Mueve la aplicación a la carpeta **Aplicaciones**.
3. Abre Visual Studio Code.
4. Instala las extensiones **Python** y **Jupyter**, ambas publicadas por Microsoft.

Si macOS muestra una advertencia la primera vez que abras Visual Studio Code, confirma que deseas abrir la aplicación descargada.

## 5. Descargar el repositorio

Se recomienda guardar el repositorio en una carpeta local que no esté sincronizada automáticamente con iCloud.

En Terminal ejecuta:

```bash
mkdir -p "$HOME/Developer"
cd "$HOME/Developer"
git clone https://github.com/cesar-martinez-torres/Energia_Renovable.git
cd Energia_Renovable
```

Si ya descargaste el repositorio, abre Terminal y cambia a su carpeta. Por ejemplo:

```bash
cd "$HOME/Developer/Energia_Renovable"
```

## 6. Crear el ambiente virtual

Desde la carpeta principal del repositorio ejecuta:

```bash
bash setup_mac.sh
```

El instalador realizará automáticamente las siguientes acciones:

1. Comprobar que Python 3.13 esté disponible.
2. Crear el ambiente virtual `.venv`.
3. Instalar las bibliotecas del curso.
4. Registrar el kernel de Jupyter.
5. Comprobar que las bibliotecas principales funcionen.

La instalación inicial puede tardar varios minutos. Debe finalizar con el mensaje:

```text
Entorno verificado correctamente.
```

Si vuelves a ejecutar `bash setup_mac.sh`, el instalador reutilizará y actualizará el ambiente existente.

## 7. Verificar la instalación

Ejecuta:

```bash
./.venv/bin/python ./00_verificar_entorno.py
```

La última línea debe decir:

```text
Entorno listo para el curso.
```

## 8. Abrir el cuaderno del curso

1. Abre Visual Studio Code.
2. Selecciona **File > Open Folder**.
3. Abre la carpeta `Energia_Renovable`.
4. Abre `01_evaluacion_economica_LCOE.ipynb`.
5. En la esquina superior derecha selecciona el kernel **Python (Energia Renovable 2026)**.
6. Ejecuta las celdas en orden.

Para confirmar que el cuaderno utiliza el ambiente correcto, puedes ejecutar:

```python
import sys
print(sys.executable)
```

La ruta mostrada debe terminar en:

```text
Energia_Renovable/.venv/bin/python
```

## 9. Usar Jupyter Lab (opcional)

Desde la carpeta del repositorio ejecuta:

```bash
./.venv/bin/python -m jupyter lab
```

Para detener Jupyter Lab, regresa a Terminal y presiona `Control+C`.

## 10. Actualizar el material del curso

Cuando se publiquen nuevos ejercicios, abre Terminal dentro del repositorio y ejecuta:

```bash
git pull
bash setup_mac.sh
```

El segundo comando instala cualquier herramienta nueva añadida al curso.

## Problemas frecuentes

### No se encontró Python 3.13

Comprueba:

```bash
python3.13 --version
```

Si el comando no existe, vuelve a instalar Python 3.13 desde la página oficial y abre una Terminal nueva.

### No aparece el kernel

Cierra y vuelve a abrir Visual Studio Code. Después abre nuevamente el cuaderno y busca **Python (Energia Renovable 2026)**.

### El ambiente virtual está dañado

1. Cierra Visual Studio Code y Jupyter.
2. Mueve la carpeta `.venv` a la Papelera desde Finder.
3. Abre Terminal en el repositorio.
4. Ejecuta nuevamente `bash setup_mac.sh`.

### Se cambió de una Mac Intel a una Mac con Apple Silicon

No copies la carpeta `.venv` de una computadora a otra. Clona el repositorio y ejecuta `bash setup_mac.sh` para crear un ambiente compatible con la nueva computadora.

### La ruta contiene espacios

Escribe la ruta entre comillas. Por ejemplo:

```bash
cd "$HOME/Mis cursos/Energia_Renovable"
```
