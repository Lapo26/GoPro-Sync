"""Biblioteca personal: favoritos, etiquetas y momentos marcados a mano.

Se guarda en library.json dentro de la carpeta de configuracion. Cada archivo
se identifica por nombre + tamano (no por la ruta): asi la marca sigue
valiendo si el archivo se mueve de carpeta o se mira la copia del disco de
respaldo. Nunca se modifica el archivo de video/foto."""
import json
import os
import threading

from .env import CONFIG_DIR

_LIB_FILE = os.path.join(CONFIG_DIR, "library.json")


def media_key(path):
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    return f"{os.path.basename(path).lower()}|{size}"


class Library:
    def __init__(self, path=_LIB_FILE):
        self.path = path
        self.lock = threading.RLock()
        self.data = None
        self._keys = {}          # ruta -> clave (evita volver a consultar el tamano)

    def _load(self):
        if self.data is None:
            try:
                with open(self.path, encoding="utf-8") as f:
                    self.data = json.load(f)
            except Exception:
                self.data = {}
            self.data.setdefault("favorites", [])
            self.data.setdefault("tags", {})
            self.data.setdefault("marks", {})
            self._fav = set(self.data["favorites"])
        return self.data

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            self.data["favorites"] = sorted(self._fav)
            tmp = self.path + ".part"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except Exception as e:
            print("[Biblioteca]", e)

    def key(self, path):
        k = self._keys.get(path)
        if k is None:
            k = media_key(path)
            self._keys[path] = k
        return k

    # ---- favoritos
    def is_favorite(self, path):
        with self.lock:
            self._load()
            return self.key(path) in self._fav

    def favorites_among(self, paths):
        with self.lock:
            self._load()
            return {p for p in paths if self.key(p) in self._fav}

    def set_favorite(self, paths, value):
        with self.lock:
            self._load()
            for p in paths:
                (self._fav.add if value else self._fav.discard)(self.key(p))
            self._save()

    def toggle_favorite(self, path):
        new = not self.is_favorite(path)
        self.set_favorite([path], new)
        return new

    # ---- etiquetas
    def tags_of(self, path):
        with self.lock:
            return list(self._load()["tags"].get(self.key(path), []))

    def tags_among(self, paths):
        with self.lock:
            tags = self._load()["tags"]
            return {p: tags[self.key(p)] for p in paths if tags.get(self.key(p))}

    def set_tags(self, paths, tags):
        clean = []
        for t in tags:
            t = " ".join(str(t).split())[:40]
            if t and t.lower() not in [c.lower() for c in clean]:
                clean.append(t)
        with self.lock:
            data = self._load()["tags"]
            for p in paths:
                if clean:
                    data[self.key(p)] = list(clean)
                else:
                    data.pop(self.key(p), None)
            self._save()

    def add_tag(self, paths, tag):
        with self.lock:
            for p in paths:
                cur = self.tags_of(p)
                if tag.lower() not in [c.lower() for c in cur]:
                    self.set_tags([p], cur + [tag])

    def remove_tag(self, paths, tag):
        with self.lock:
            for p in paths:
                self.set_tags([p], [t for t in self.tags_of(p) if t.lower() != tag.lower()])

    def all_tags(self):
        with self.lock:
            seen = {}
            for lst in self._load()["tags"].values():
                for t in lst:
                    seen.setdefault(t.lower(), t)
            return sorted(seen.values(), key=str.lower)

    # ---- momentos marcados a mano (ademas de los HiLight de la camara)
    def marks_of(self, path):
        with self.lock:
            return sorted(self._load()["marks"].get(self.key(path), []))

    def add_mark(self, path, ms):
        with self.lock:
            data = self._load()["marks"]
            lst = data.setdefault(self.key(path), [])
            if all(abs(m - ms) > 500 for m in lst):
                lst.append(int(ms))
                lst.sort()
            self._save()

    def remove_mark(self, path, ms):
        with self.lock:
            data = self._load()["marks"]
            lst = [m for m in data.get(self.key(path), []) if abs(m - ms) > 500]
            if lst:
                data[self.key(path)] = lst
            else:
                data.pop(self.key(path), None)
            self._save()


LIBRARY = Library()
