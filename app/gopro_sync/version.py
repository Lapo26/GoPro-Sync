"""Version de la app y repositorio de GitHub."""

# Version de la app. La actualiza release.ps1 (y el workflow de release al compilar).
APP_VERSION = "1.5.1"
# Repositorio de GitHub de donde salen las versiones (si se vuelve a renombrar, GitHub
# redirige la API, pero conviene mantener esto al dia).
GITHUB_REPO = "Lapo26/GoPro-Sync"
# Firma de los instaladores (Authenticode). El actualizador de Windows exige que
# el instalador descargado este firmado y que el "Subject" del certificado sea
# este (copiarlo tal cual de Propiedades > Firmas digitales, o de
# (Get-AuthenticodeSignature .\GoProSync-Setup-X.exe).SignerCertificate.Subject).
# Vacio = sin firma fija: si la app instalada ya esta firmada, se exige el mismo
# firmante que ella; si no lo esta, se acepta un instalador sin firma (solo con
# el SHA-256 del Release).
UPDATE_SIGNER_SUBJECT = ""
