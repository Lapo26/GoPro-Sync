"""GoPro Sync: punto de entrada.

El codigo esta en el paquete gopro_sync/ (un modulo por responsabilidad):
  gopro_sync/app.py          arranque y autoprueba (--selftest)
  gopro_sync/ui/             interfaz (ventana principal, galerias, dialogos...)
  gopro_sync/camera.py, mtp.py, workers.py, fileops.py, media.py, videotools.py ...
"""
import os
import sys

# Permite ejecutar "python app/main.py" desde cualquier carpeta
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if __name__ == "__main__" and "--gps-index" in sys.argv:
    # Proceso de fondo del mapa global: lee el GPS de la biblioteca y lo
    # guarda en el indice. Sin ventana ni bandeja (no pasa por la app).
    from gopro_sync.gps_index import worker_main
    sys.exit(worker_main(sys.argv))

from gopro_sync.app import main, run_selftest  # noqa: E402

if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(run_selftest())
    main()
