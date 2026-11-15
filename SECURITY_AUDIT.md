# Auditoría de seguridad — GoPro Sync

- **Fecha:** 2026-11-14
- **Alcance:** `app/` (≈20 000 líneas de Python), scripts de empaquetado (`packaging/`, `release.ps1`), instalador Inno Setup, workflow de GitHub Actions e historial de git.
- **Base revisada:** `main` @ `89dd103` **más los cambios sin commitear** del checkout principal (20 archivos modificados y 6 nuevos), copiados a la rama `security-audit`. Las correcciones se reaplicaron después sobre el estado más nuevo de `main` (commit `42b7e85`), que ya no incluye la telemetría, HiLight ni el cambio de velocidad.
- **Método:** revisión manual del código siguiendo los datos que vienen de fuera: cámara (MTP/WPD, gphoto2, HTTP Open GoPro, BLE), tarjeta SD (`MISC/version.txt`, MP4/JPG), servidores de teselas, API de GitHub (actualizaciones) y la cadena de build/release. No se ejecutó la app ni se hicieron pruebas dinámicas.

## Resumen

| # | Severidad | Hallazgo | Archivo |
|---|-----------|----------|---------|
| 1 | **Alta** (impacto) / prob. baja | El actualizador instala en silencio un `.exe` cuya autenticidad depende solo de GitHub; los binarios no tienen firma | `updates.py`, `release.yml` |
| 2 | **Media** | Path traversal: el nombre del dispositivo (de `version.txt` o del nombre MTP) se usa como carpeta de destino sin sanear | `fileops.py:76`, `workers.py:440` |
| 3 | **Media** | "Borrar de la cámara después de copiar" no comprueba que la copia tenga el tamaño completo (WPD y Explorador) | `mtp.py:575`, `mtp.py:185` |
| 4 | **Media-Baja** | Cadena de suministro del build: dependencias sin fijar ni hashes, `appimagetool` "continuous" sin checksum, Inno Setup sin versión fija | `requirements.txt`, `build_unix.sh:66`, `release.yml:41` |
| 5 | Baja | Se borra de la cámara un archivo si en el destino hay otro con el **mismo nombre y tamaño**, sin comparar el contenido | `workers.py:361` |
| 6 | Baja | Peticiones HTTP sin autenticación a `172.2x.y.51:8080` en cualquier red 172.20–29 (Docker, WSL, VPN), incluida una que cambia un ajuste | `camera.py:511`, `camera.py:664` |
| 7 | Baja | Patrón de inyección de scripts en Actions: `${{ github.ref_name }}` dentro de `run:` | `release.yml:32`, `release.yml:117` |
| 8 | Baja | En Linux, carpetas con nombre fijo en `/tmp` compartido (vista previa y caché de teselas) | `camera_page.py:267`, `map.py:63` |
| 9 | Baja | Named pipe de instancia única con nombre predecible y sin restricción de acceso | `app.py:28` |
| 10 | Baja | Ejecutables llamados por nombre (`powershell`, `ipconfig`, `explorer`) en vez de ruta absoluta | `mtp.py`, `camera.py`, `workers.py`, `winshell.py` |
| 11 | Baja | `is_safe_camera_name` deja pasar nombres reservados de Windows (`CON.MP4`, `NUL.JPG`) y nombres que terminan en punto o espacio | `fileops.py:35` |
| 12 | Baja | Borrado por lotes con `FolderItems.Filter()`: `;` (válido en FAT) separa patrones; el comando puede pasar el límite de 32 KB | `mtp.py:262` |
| 13 | Info / privacidad | Las coordenadas GPS de los videos se envían a Esri/OSM/OpenTopoMap al ver el mapa; el historial de git incluye rutas locales con nombre de usuario | `map.py`, historial |

## Estado de las correcciones (rama `security-audit`)

| # | Estado | Qué se cambió |
|---|--------|---------------|
| 1 | **Corregido en código; necesita tu certificado** | `updates.py`: antes de ejecutar el instalador se comprueba la firma Authenticode (`Get-AuthenticodeSignature`, con la ruta por variable de entorno). Se rechaza siempre una firma rota (`HashMismatch`…). Se exige el firmante de `UPDATE_SIGNER_SUBJECT` (`version.py`) o, si está vacío, el mismo firmante que la app instalada cuando esta ya está firmada. `build.ps1` + `GoProSync.iss` firman `GoProSync.exe`, el instalador y el desinstalador si existen los secretos `WINDOWS_SIGN_PFX_B64` y `WINDOWS_SIGN_PASSWORD`. El workflow genera atestaciones de procedencia (`gh attestation verify`). |
| 2 | Corregido | `safe_device_name()` sanea el nombre del dispositivo donde se lee (`camera.py`) y en `quik_camera_folder`; `_seq_subdir` comprueba con `is_inside()` que el destino final quede dentro de la biblioteca. |
| 3 | Corregido | `WpdCameraSession.copy_to(size=…)` lanza error si llegan menos bytes; la copia por el Explorador exige el tamaño exacto en disco (si no, se borra la copia parcial y el archivo no se marca para borrar de la cámara). Texto `err_incomplete_copy` en es/en/de. |
| 4 | Corregido | `requirements.lock` universal con hashes (uv, Python 3.10+, Win/macOS/Linux); `build.ps1`, `build_unix.sh`, `try.ps1` e `install_requirements.ps1` instalan con `--require-hashes`. `appimagetool` fijado a 1.9.1 con SHA-256; Inno Setup fijado a 6.7.1. |
| 5 | Corregido | `_safe_to_delete_existing()`: para borrar del origen un archivo que "ya estaba", además de nombre y tamaño tiene que coincidir la fecha (±2 s). Sin fecha conocida no se borra. |
| 6 | Corregido | Solo se consideran IPs locales `172.2X.1YZ.*` (formato de Open GoPro por USB) y solo se manda el cambio de ajuste si la respuesta tiene `status` y `settings`; lecturas HTTP limitadas a 1 MB. |
| 7 | Corregido | `TAG_NAME`/`REPO` por `env:` en todos los `run:`. |
| 8 | Corregido | Nuevo `CACHE_DIR` por usuario (`%LOCALAPPDATA%`, `~/Library/Caches`, `$XDG_CACHE_HOME`) para la vista previa y las teselas; en Windows las teselas siguen en la misma carpeta. |
| 9 | Corregido | Nombre del canal = hash de usuario + clave aleatoria guardada en la carpeta de configuración (`instance.key`), y `UserAccessOption`. |
| 10 | Corregido | `POWERSHELL_EXE`, `IPCONFIG_EXE`, `EXPLORER_EXE` con ruta completa en `env.py`. |
| 11 | Corregido | `is_safe_camera_name` rechaza nombres reservados y los que terminan en punto o espacio. |
| 12 | Corregido | `Filter()` se salta si algún nombre tiene `; * ? [ ]` y verifica nombre por nombre antes de borrar; los scripts de más de 30 KB se ejecutan desde un `.ps1` temporal (`run_powershell_script`). |
| 13 | Corregido (mapas) / **pendiente tuyo** (historial) | Nuevo ajuste "Descargar mapas de internet" (encendido por defecto, con explicación); apagado no se pide ninguna tesela y el mapa lo indica. El historial de git **no** se reescribió: es destructivo y requiere un force push coordinado. |

**Verificado:** compilación de todos los módulos; pyflakes sin avisos nuevos; pruebas de las funciones nuevas (nombres hostiles, traversal, borrado, copias cortadas, parser real de PowerShell con comillas tipográficas e inyección, script de más de 30 KB, firmas válida, ausente, de otro firmante y rota); `--selftest` de la app; instancia única, Ajustes y mapa sin conexión con una configuración aislada; sintaxis de los `.ps1`, `build_unix.sh` y el YAML.

**No verificado:** sincronización con una cámara real (MTP/WPD y Open GoPro por USB), compilación del instalador con Inno Setup y firma (no hay ISCC ni certificado en este equipo), y una ejecución real del workflow en GitHub.

**Lo que tienes que hacer tú para el #1:** conseguir un certificado de firma de código (o Azure Trusted Signing), cargar los secretos `WINDOWS_SIGN_PFX_B64`/`WINDOWS_SIGN_PASSWORD`, poner su `Subject` en `UPDATE_SIGNER_SUBJECT`, y en GitHub activar 2FA y una regla que proteja los tags `v*`. Mientras no haya firma, el actualizador se comporta como antes (solo el SHA-256 del Release).

---

## 1. Actualizador: la autenticidad depende solo de GitHub — **Alta (impacto)**

**Dónde:** [updates.py](app/gopro_sync/updates.py), [release.yml](.github/workflows/release.yml), [GoProSync.iss](packaging/GoProSync.iss)

**Qué hace hoy (bien):** HTTPS, comprueba que la URL empiece por `https://github.com/<repo>/releases/download/`, carpeta temporal con `mkdtemp`, nombre de versión saneado, verificación de tamaño y SHA-256, y si no hay hash no instala en silencio.

**El problema:** el SHA-256 viene del campo `digest` de **la misma respuesta de la API** que da la URL del binario. Protege contra corrupción en la descarga, pero **no autentica al publicador**. Quien pueda publicar un Release en `Lapo26/GoPro-Sync` (cuenta o token robado, colaborador malicioso, un workflow comprometido con `contents: write`, una dependencia de build envenenada; ver #4) consigue que **todos** los clientes Windows ejecuten su `.exe` con `/SILENT` al pulsar "Actualizar". Además los instaladores y `GoProSync.exe` **no tienen firma Authenticode**, así que tampoco se puede comprobar la firma antes de ejecutar (y SmartScreen avisa a los usuarios nuevos).

Escenario: un atacante consigue un PAT con permiso `repo`, crea el tag `v9.9.9` y sube un instalador malicioso. GitHub calcula el `digest` del archivo malicioso, la app lo acepta y lo ejecuta.

**Recomendaciones (de mayor a menor efecto):**
1. **Firmar el código** (Authenticode, por ejemplo con Azure Trusted Signing o un certificado OV/EV) en CI, y antes de `Popen` comprobar con `WinVerifyTrust` (o `Get-AuthenticodeSignature`) que la firma es válida **y** que el sujeto o la huella del certificado es el esperado.
2. Como alternativa o complemento: firma propia **Ed25519/minisign** del instalador, con la clave privada **fuera** de GitHub y la pública incluida en la app. El cliente verifica `GoProSync-Setup-X.exe.minisig` antes de instalar.
3. Endurecer el repositorio: 2FA obligatorio, reglas de protección de tags `v*` (solo el mantenedor), entorno `release` con aprobación manual en el job que publica, y [artifact attestations](https://docs.github.com/actions/security-guides/using-artifact-attestations) (`actions/attest-build-provenance`).
4. Opcional: no instalar en silencio en el primer arranque tras la descarga; mostrar el editor de la firma.

---

## 2. Path traversal por el nombre del dispositivo — **Media**

**Dónde:** [fileops.py:76](app/gopro_sync/fileops.py:76) `quik_camera_folder`, usado en [workers.py:440](app/gopro_sync/workers.py:440), [workers.py:509](app/gopro_sync/workers.py:509) y [workers.py:1114](app/gopro_sync/workers.py:1114).

`LAST_DEVICE_NAME["name"]` sale de datos externos sin sanear:
- [camera.py:392](app/gopro_sync/camera.py:392): el valor de `"camera type"` en `MISC/version.txt` de **cualquier tarjeta SD o carpeta importada**.
- [camera.py:369](app/gopro_sync/camera.py:369): el nombre amigable del dispositivo MTP (lo controla el firmware del dispositivo USB).
- `GPHOTO.autodetect()` en macOS/Linux.

Después: `cam_folder = f"{device_name} {num}"` y `dest_dir = os.path.join(target, fecha, cam_folder)`. Los **nombres de archivo** sí se filtran (`safe_camera_entries`), pero este componente de carpeta no.

Escenario: una tarjeta SD con `MISC/version.txt` que contiene `"camera type":"C:\\Users\\Public\\HERO9 x"` (o `HERO9\\..\\..\\..\\algo`). Al importarla con "ordenar por fechas" activo, `os.path.join` descarta el prefijo (ruta absoluta) y los archivos `.MP4/.JPG/.GPR` se escriben en `C:\Users\Public\HERO9 x 1\`. El componente final siempre termina en `" N"` y solo se escriben extensiones de medios, por eso el impacto es limitado: escritura de archivos fuera de la biblioteca y creación de carpetas en cualquier sitio con permisos del usuario, sin sobrescribir (hay renombrado `_1`). Lo mismo con un dispositivo USB que se anuncie como `HERO…` con `\` o `..` en el nombre.

**Corrección:**
```python
_SAFE_DEVICE_RE = re.compile(r"[^A-Za-z0-9 ._()-]+")

def safe_device_name(name):
    name = _SAFE_DEVICE_RE.sub("_", (name or "")).strip(" .")[:64]
    return name or None
```
Usarla al asignar `LAST_DEVICE_NAME`. Como defensa en profundidad, antes de `os.makedirs`/copiar, comprobar que el destino queda dentro de la biblioteca:
```python
def inside(path, root):
    path, root = os.path.realpath(path), os.path.realpath(root)
    return os.path.commonpath([path, root]) == root
```

---

## 3. Borrado de la cámara sin verificar el tamaño de la copia — **Media** (integridad de datos)

**Dónde:**
- [mtp.py:575](app/gopro_sync/mtp.py:575) `WpdCameraSession.copy_to`: si `RemoteRead` devuelve 0 bytes antes de tiempo (cable que se mueve, cámara que se duerme), sale del bucle, hace `os.replace(part, dest)` y devuelve `True` **sin comparar `done` con el tamaño esperado**.
- [mtp.py:185](app/gopro_sync/mtp.py:185) (copia por el Explorador): éxito = `Test-Path $destPath` y tamaño estable dos veces. Con la preasignación de MTP el tamaño puede quedar "estable" aunque la copia esté incompleta.
- [workers.py:1152](app/gopro_sync/workers.py:1152): con `delete_after` esos archivos se añaden a `to_delete_by_folder` y **se borran de la cámara de forma permanente** (MTP no tiene papelera).

La ruta gphoto2 sí lo hace bien (`size_mismatch` en `camera.py:263`).

**Corrección:** en `copy_to`, `if size and done != size: raise WpdError("incompleto")` (pasar el tamaño esperado). En `_copy_via_shell`/el bloque de Phase 3, exigir `os.path.getsize(root_dest_path) == size` antes de considerar la copia correcta. Antes de borrar, idealmente revalidar el tamaño en disco de cada archivo.

---

## 4. Cadena de suministro del build — **Media-Baja**

- [app/requirements.txt](app/requirements.txt) y [requirements-dev.txt](requirements-dev.txt) usan `>=` sin hashes; CI instala siempre "lo último" (PySide6, bleak, comtypes, imageio-ffmpeg, **pyinstaller**). Una versión maliciosa o comprometida en PyPI acaba dentro del instalador firmado (o no) y, por el #1, en todos los clientes. Además `setup-python` guarda en caché pip con esa clave.
- [build_unix.sh:66](packaging/build_unix.sh:66) descarga `appimagetool` del release **`continuous`** (cambia en cada commit) y lo ejecuta sin checksum.
- [release.yml:41](.github/workflows/release.yml:41) `choco install innosetup` sin versión.
- `imageio-ffmpeg` incluye un binario `ffmpeg` que procesa archivos no confiables: conviene fijarlo y actualizarlo a propósito.

**Bien hecho:** las Actions están fijadas por SHA y `permissions: contents: read` por defecto, con `write` solo en el job `release`.

**Corrección:** un lock con hashes (`pip-compile --generate-hashes` → `requirements.lock`, e instalar con `pip install --require-hashes -r requirements.lock`); fijar `appimagetool` a un release concreto y verificar el `sha256sum`; `choco install innosetup --version 6.x.y`; Dependabot/Renovate para las subidas controladas.

---

## 5. Deduplicación solo por nombre y tamaño antes de borrar — **Baja**

[workers.py:361](app/gopro_sync/workers.py:361) `_already_there`: si en la biblioteca hay un archivo con el mismo nombre (sin distinguir mayúsculas, y también con los sufijos `_N`) y el mismo tamaño, el de la cámara se da por copiado y, con `delete_after`, **se borra sin copiarlo**. Las GoPro reinician la numeración (`GX010001.MP4`, `GOPR0001.JPG`) al formatear o al cambiar de cámara, así que los choques de nombre son habituales. Que coincida también el tamaño es raro, pero si pasa se pierde un archivo. Para borrar, convendría exigir además la fecha de captura o un hash parcial (primeros y últimos MB).

## 6. HTTP Open GoPro hacia cualquier red 172.20–29 — **Baja**

[camera.py:664](app/gopro_sync/camera.py:664) toma **cualquier** IPv4 local `172.2x.*` (las redes de Docker, WSL/Hyper-V y muchas VPN corporativas caen en ese rango) y hace peticiones a `.51:8080`. Cada 15 s, [camera.py:536](app/gopro_sync/camera.py:536) envía además `GET /gopro/camera/setting?setting=112&option=1` a ese host. Cualquier equipo con la IP `.51` en esas redes recibe tráfico y puede responder datos falsos de batería. Es HTTP sin autenticación (limitación de Open GoPro por USB). Conviene filtrar por el adaptador de red de la GoPro (descripción "GoPro" / RNDIS en `Get-NetAdapter`) en vez de por rango de IP, y no mandar el cambio de ajuste hasta haber confirmado que es una GoPro (`/gopro/camera/info`).

## 7. `${{ github.ref_name }}` dentro de `run:` — **Baja**

[release.yml:32](.github/workflows/release.yml:32) y [release.yml:117](.github/workflows/release.yml:117). Hoy solo el mantenedor puede crear tags, pero es el patrón clásico de inyección de comandos en Actions. Mejor pasarlo por entorno:
```yaml
env:
  TAG: ${{ github.ref_name }}
run: gh release create "$TAG" out/*/* ...
```

## 8. Rutas fijas en `/tmp` compartido (Linux) — **Baja**

- [camera_page.py:267](app/gopro_sync/ui/camera_page.py:267): `tempfile.gettempdir()/GoProSync-preview/<nombre>`. En Linux es `/tmp`, compartido entre usuarios: otro usuario puede crear la carpeta antes y poner symlinks (la descarga los sigue y sobrescribe archivos de la víctima) o archivos falsos (se abren sin descargar si ya existen y tienen tamaño > 0).
- [map.py:63](app/gopro_sync/ui/map.py:63): `LOCALAPPDATA` no existe en Linux ni en macOS, así que la caché de teselas cae en `/tmp/GoProSyncPro/tiles` (envenenamiento de la caché o symlinks).

En Windows y macOS `gettempdir()` es por usuario, así que el riesgo es solo de Linux. Usar `CONFIG_DIR`/`XDG_CACHE_HOME` o `tempfile.mkdtemp()`.

## 9. Instancia única con named pipe predecible — **Baja**

[app.py:28](app/gopro_sync/app.py:28): `GoProSyncPro-<usuario>`, `removeServer` + `listen` sin opciones de acceso. En un equipo compartido otro usuario puede crear el pipe antes: la app de la víctima "detecta otra copia" y se cierra (DoS), o recibe conexiones ajenas. Usar `server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)` antes de `listen` e incluir el ID de sesión en el nombre.

## 10. Ejecutables por nombre — **Baja**

`subprocess` con `['powershell', …]`, `['ipconfig']`, `['explorer', …]`. En Windows, `CreateProcess` busca primero en la carpeta del ejecutable y en el directorio actual. La instalación "solo para mí" va a `%LOCALAPPDATA%\Programs\GoPro Sync` (escribible por el usuario). El riesgo es bajo (hace falta poder escribir como ese usuario), pero es fácil de eliminar: `os.path.join(os.environ["SystemRoot"], "System32", "WindowsPowerShell", "v1.0", "powershell.exe")`, etc.

## 11. Nombres reservados de Windows — **Baja**

[fileops.py:35](app/gopro_sync/fileops.py:35) no rechaza `CON`, `PRN`, `AUX`, `NUL`, `COM1-9`, `LPT1-9` (con o sin extensión) ni nombres que terminan en punto o espacio (Windows los recorta, lo que provoca colisiones). Un dispositivo MTP malicioso podría provocar errores o escrituras a dispositivos. Añadir:
```python
_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$", re.I)
... and not _RESERVED.match(name) and not name.endswith((".", " "))
```

## 12. Borrado por lotes con `Filter()` — **Baja**

[mtp.py:262](app/gopro_sync/mtp.py:262): la lista se une con `;`, que en FAT/exFAT es un carácter válido en nombres largos, y `Filter` interpreta `*`/`?`. La comprobación `Count -eq $wantedCount` limita el riesgo de borrar archivos de más, pero no lo elimina si se compensan. Además el script va en la línea de comandos (`-Command`), con un límite de unas 32 000 caracteres: con cientos de archivos el borrado falla. Pasar el script por `-EncodedCommand` o por stdin/archivo temporal, y confirmar cada elemento por nombre exacto antes de `InvokeVerbEx`.

## 13. Privacidad — **Informativo**

- Al ver el mapa de un video con GPS se piden teselas a `server.arcgisonline.com`, `tile.openstreetmap.org` y `*.tile.opentopomap.org`: esos servicios ven **dónde se grabó** cada video (con IP y `User-Agent: GoProSync/x`). Conviene avisarlo en Ajustes u ofrecer "mapa sin conexión".
- El historial público de git contiene archivos borrados (`GoPro Sync Version 0.5/*.lnk`, `gopro_akku_diagnose.txt`, `build/…`) con rutas locales tipo `C:\Users\<usuario>\Desktop\…`. Solo revelan el nombre de usuario local. Si importa, `git filter-repo` (reescribe el historial: hay que coordinarlo).

---

## Lo que está bien

- **Escape de PowerShell muy cuidado** (`_ps_str`): duplica `'` y también las comillas tipográficas U+2018–U+201B, que PowerShell trata como comillas. Según la lectura del código, un nombre como `a’; calc; ’b.MP4` queda neutralizado (no se probó ejecutándolo).
- Nombres de archivo de la cámara filtrados (`safe_camera_entries`) en todas las rutas de listado (WPD/Shell/gphoto2).
- Ninguna llamada usa `shell=True`, `eval`, `exec`, `pickle` ni `yaml.load`; todos los `subprocess` reciben listas de argumentos.
- Listas `concat` de ffmpeg con el escape correcto (`'\''`); filtros construidos solo con números.
- Parsers MP4/GPMF/EXIF con límites (`size < hdr`, lecturas topadas a 4/16/64 MB) y excepciones capturadas: un archivo malformado no tumba la app.
- WPD se abre en modo **solo lectura** (`GENERIC_READ`).
- Instalador con `PrivilegesRequired=lowest` (sin admin por defecto); el autoarranque usa la ruta entre comillas.
- Workflow con Actions fijadas por SHA y permisos mínimos.
- `release.ps1` valida la versión con regex antes de usar `Invoke-Expression`.

## Prioridad sugerida

1. **#2 y #3**: correcciones pequeñas y locales; evitan escrituras fuera de la biblioteca y pérdida de videos.
2. **#4 y #7**: lockfile con hashes, `appimagetool` fijado, `env:` en el workflow (una tarde).
3. **#1**: firma de código y verificación en el actualizador (requiere certificado o clave; es lo de mayor impacto a medio plazo).
4. El resto, como endurecimiento cuando se toque ese código.
