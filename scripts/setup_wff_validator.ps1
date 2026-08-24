param(
    [string]$Revision = "44b1855d445686ac8de5dbc95003d6f8e6623643",
    [string]$Destination = "tools/wff-validator/wff-validator.jar"
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$destinationPath = Join-Path $repoRoot $Destination
$sourceRoot = Join-Path $repoRoot "tools/wff-validator/source"

if (-not (Test-Path (Join-Path $sourceRoot ".git"))) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $sourceRoot) | Out-Null
    git clone https://github.com/google/watchface.git $sourceRoot
}

git -C $sourceRoot fetch --depth 1 origin $Revision
git -C $sourceRoot checkout --detach $Revision

$previousEncoding = $env:JAVA_TOOL_OPTIONS
$env:JAVA_TOOL_OPTIONS = "-Dfile.encoding=UTF-8"
try {
    & (Join-Path $sourceRoot "third_party/wff/gradlew.bat") `
        -p (Join-Path $sourceRoot "third_party/wff") `
        :specification:validator:executable-jar
    if ($LASTEXITCODE -ne 0) {
        throw "Google WFF validator build failed with exit code $LASTEXITCODE"
    }
}
finally {
    $env:JAVA_TOOL_OPTIONS = $previousEncoding
}

$builtJar = Join-Path $sourceRoot "third_party/wff/specification/validator/build/libs/wff-validator.jar"
if (-not (Test-Path $builtJar)) {
    throw "Google WFF validator output was not produced: $builtJar"
}
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destinationPath) | Out-Null
Copy-Item -Force $builtJar $destinationPath
Write-Output $destinationPath
