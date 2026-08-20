param(
    [Parameter(Mandatory = $true)][string]$XmlPath,
    [Parameter(Mandatory = $true)][string]$ValidatorJar
)

& java -jar $ValidatorJar 1 $XmlPath
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

