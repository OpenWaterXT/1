# World Para Swimming — Master Lists Archive

Recopilador de las **Master Lists de clasificación de World Para Swimming** correspondientes a los últimos 15 años (2012–2026).

## Qué hace

- Busca documentos oficiales publicados en dominios del IPC / World Para Swimming.
- Consulta también el índice histórico de Internet Archive (Wayback Machine) para localizar versiones retiradas de la web actual.
- Descarga PDF, XLS/XLSX, CSV y documentos equivalentes.
- Organiza los resultados por año en `data/<año>/`.
- Genera `data/manifest.csv` con año, fecha de captura, URL oficial original, URL archivada, nombre del fichero, formato y hash SHA-256.
- Evita duplicados mediante hash.

## Ejecución

```bash
python -m pip install -r requirements.txt
python scrape_master_lists.py --start-year 2012 --end-year 2026
```

También se puede ejecutar manualmente desde la pestaña **Actions** mediante el workflow `Collect WPS Master Lists`.

## Criterio documental

El repositorio conserva únicamente documentos cuya URL, nombre o contenido identificable esté relacionado con `Swimming`, `World Para Swimming`, `IPC Swimming`, `classification` y `master list`. El manifiesto mantiene la URL oficial original para poder comprobar la procedencia.

## Aviso

Los documentos pertenecen a sus organismos editores. Este repositorio funciona como archivo técnico y conserva la atribución y procedencia de cada fichero.
