[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$RequiredMajor = 3
$RequiredMinor = 13
$EnvironmentDirectory = Join-Path $PSScriptRoot ".venv"
$RequirementsFile = Join-Path $PSScriptRoot "requirements.txt"
$KernelName = "energia-renovable-2026"
$KernelDisplayName = "Python (Energia Renovable 2026)"

function Get-CoursePython {
    $candidates = @(
        @{ Command = "py"; Arguments = @("-3.13") },
        @{ Command = "python3.13"; Arguments = @() },
        @{ Command = "python"; Arguments = @() }
    )

    foreach ($candidate in $candidates) {
        if (-not (Get-Command $candidate.Command -ErrorAction SilentlyContinue)) {
            continue
        }

        try {
            $version = & $candidate.Command @($candidate.Arguments) -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
            if ($LASTEXITCODE -eq 0 -and $version.Trim() -eq "$RequiredMajor.$RequiredMinor") {
                return $candidate
            }
        }
        catch {
            continue
        }
    }

    throw "No se encontro Python 3.13. Instalalo y vuelve a ejecutar este archivo."
}

if (-not (Test-Path -LiteralPath $RequirementsFile)) {
    throw "No se encontro requirements.txt junto a setup.ps1."
}

$coursePython = Get-CoursePython
Write-Host "Python 3.13 encontrado."

if (-not (Test-Path -LiteralPath $EnvironmentDirectory)) {
    Write-Host "Creando el ambiente virtual .venv..."
    & $coursePython.Command @($coursePython.Arguments) -m venv $EnvironmentDirectory
    if ($LASTEXITCODE -ne 0) { throw "No fue posible crear el ambiente virtual." }
}
else {
    Write-Host "El ambiente virtual .venv ya existe; se actualizara."
}

$EnvironmentPython = Join-Path $EnvironmentDirectory "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $EnvironmentPython)) {
    throw "El ambiente virtual no contiene Scripts\python.exe. Elimina .venv y vuelve a intentarlo."
}

Write-Host "Actualizando las herramientas de instalacion..."
& $EnvironmentPython -m pip install --upgrade pip setuptools wheel
if ($LASTEXITCODE -ne 0) { throw "Fallo la actualizacion de pip." }

Write-Host "Instalando las herramientas del curso..."
& $EnvironmentPython -m pip install --upgrade -r $RequirementsFile
if ($LASTEXITCODE -ne 0) { throw "Fallo la instalacion de requirements.txt." }

Write-Host "Registrando el kernel de Jupyter..."
& $EnvironmentPython -m ipykernel install --user --name $KernelName --display-name $KernelDisplayName
if ($LASTEXITCODE -ne 0) { throw "Fallo el registro del kernel de Jupyter." }

Write-Host "Verificando las bibliotecas principales..."
& $EnvironmentPython -c "import matplotlib, numpy, numpy_financial, openpyxl, pandas, pvlib, requests, scipy; print('Entorno verificado correctamente.')"
if ($LASTEXITCODE -ne 0) { throw "La verificacion final del entorno fallo." }

Write-Host ""
Write-Host "Instalacion terminada."
Write-Host "Kernel: $KernelDisplayName"
Write-Host "Para comprobar Python, ejecuta:"
Write-Host ".\.venv\Scripts\python.exe .\00_verificar_entorno.py"
Write-Host "Para iniciar Jupyter Lab, ejecuta:"
Write-Host ".\.venv\Scripts\python.exe -m jupyter lab"

