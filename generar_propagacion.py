#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generar_propagacion.py
-----------------------------------------------------------------
Se ejecuta SOLO en GitHub (Actions), todos los días, sin que tengas que instalar nada.
Genera un GIF animado con la previsión de propagación en 11 m (27,1 MHz) desde tu QTH
para las próximas 48 horas y una página web sencilla que lo muestra.

  1. Lee el flujo solar previsto (NOAA SWPC) y lo convierte en SSN de cada día.
  2. Calcula con DVOACAP (motor VOACAP en Python) un mapa de fiabilidad (REL) por hora UTC.
  3. Dibuja los mapas con la escala de colores de voacap.com y los une en un GIF.
  4. Escribe todo en la carpeta "web", que el flujo de trabajo publica en GitHub Pages.

Opciones (las usa el flujo de trabajo al pulsar "Run workflow"):
  --demo      datos falsos, solo prueba el dibujo (segundos)
  --validar   un mapa con los datos de tu imagen de voacap.com (oct 2026, SSN 53, 15 UT)
  --rapido    3 horas en resolución gruesa (prueba real del motor)
  (sin nada)  ejecución completa de 48 horas

Ajustes habituales: sección 1. Para otro QTH, frecuencia o potencia, cambia esos valores.
-----------------------------------------------------------------
"""
import argparse
import datetime as dt
import io
import json
import math
import os
import re
import sys
import urllib.request
from concurrent.futures import ProcessPoolExecutor

# =================================================================
# 1. CONFIGURACIÓN
# =================================================================
REPO_DVOACAP = os.path.abspath(os.environ.get("DVOACAP_DIR", "dvoacap-python"))   # lo clona el flujo de trabajo
QTH_LAT, QTH_LON, QTH_LOC = 43.56, -8.12, "IN53WN"
FRECUENCIA_MHZ = 27.1                    # 11 m
MODO = "SSB"                             # SSB = 38 dB/Hz, como en voacap.com
POTENCIA_W = 80.0
RESOLUCION = "coarse"                    # "coarse" (1-2 min/mapa) | "medium" (5-10 min/mapa) | "fine"
HORAS_ADELANTE = 48                      # horas de previsión desde la hora actual
SSN_POR_DEFECTO = 53.0                   # si falla la descarga del flujo solar
URL_PREVISION = "https://services.swpc.noaa.gov/text/27-day-outlook.txt"

CARPETA_SALIDA = os.environ.get("PROP_SALIDA", "web")   # lo que se publica en GitHub Pages
CARPETA_CACHE = "cache"                  # mapas ya calculados (no se publica)
NOMBRE_GIF = "propagacion_11m_IN53WN.gif"
MS_POR_FOTOGRAMA = 700
ANCHO_PX = 900

# Colores de la escala REL de voacap.com (de 0-10 % a 90-100 %)
COLORES_REL = ["#ffffff", "#bdefff", "#70d3ff", "#00bfff", "#22b14c",
               "#b4ff00", "#ffff00", "#ffc828", "#ff7f00", "#ff0000"]


# =================================================================
# 2. FLUJO SOLAR Y SSN
# =================================================================
def flujo_a_ssn(flujo):
    """Convierte el flujo solar F10.7 en número de manchas (relación de Covington)."""
    a, b, c = 0.00089, 0.728, 63.7 - flujo
    return max(0.0, (-b + math.sqrt(b * b - 4 * a * c)) / (2 * a))


def leer_prevision_flujo():
    """Devuelve {fecha: flujo} con la previsión a 27 días de NOAA, o {} si no se puede leer."""
    try:
        with urllib.request.urlopen(URL_PREVISION, timeout=30) as r:
            texto = r.read().decode("utf-8", "replace")
    except Exception as e:
        print("Aviso: no se pudo leer la previsión de flujo solar (%s). Se usa SSN %s." % (e, SSN_POR_DEFECTO))
        return {}
    meses = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                                          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
    previsto = {}
    for m in re.finditer(r"^(\d{4}) ([A-Z][a-z]{2}) (\d{1,2})\s+(\d+)", texto, re.M):
        if m.group(2) in meses:
            previsto[dt.date(int(m.group(1)), meses[m.group(2)], int(m.group(3)))] = float(m.group(4))
    return previsto


def ssn_del_dia(fecha, previsto):
    """(SSN, flujo) para una fecha; si falta el dato, el más cercano o el valor por defecto."""
    if previsto:
        cercana = min(previsto, key=lambda d: abs((d - fecha).days))
        if abs((cercana - fecha).days) <= 3:
            return round(flujo_a_ssn(previsto[cercana]), 1), previsto[cercana]
    return SSN_POR_DEFECTO, None


# =================================================================
# 3. CÁLCULO (DVOACAP)
# =================================================================
def calcular_mapa(tarea):
    """Un mapa de fiabilidad. Se ejecuta en procesos aparte. tarea = (mes, hora, ssn, resolucion)."""
    mes, hora, ssn, resolucion = tarea
    sys.path.insert(0, REPO_DVOACAP)
    sys.path.insert(0, os.path.join(REPO_DVOACAP, "src"))
    from Dashboard.generate_propagation_maps import generate_propagation_map
    datos = generate_propagation_map(
        tx_lat=QTH_LAT, tx_lon=QTH_LON, frequency=FRECUENCIA_MHZ, mode=MODO,
        utc_hour=hora, month=mes, ssn=ssn, tx_power=POTENCIA_W,
        map_range_deg=90.0, resolution=resolucion)
    return [(p["lat"], p["lon"], p["reliability"]) for p in datos["predictions"]]


def mapa_demo(hora):
    """Datos falsos y suaves, solo para probar el dibujo sin esperar al motor."""
    import numpy as np
    puntos = []
    for lat in np.arange(-87.5, 90, 5.0):
        for lon in np.arange(-177.5, 180, 5.0):
            d = math.hypot((lon - (hora * 15 - 180)) / 60, lat / 35)
            puntos.append((float(lat), float(lon), max(0.0, 95 - 40 * d + 10 * math.sin(lon / 20))))
    return puntos


def calcular_todos(tareas, demo):
    """Calcula los mapas en paralelo, reutilizando los que ya están en la caché."""
    cache = CARPETA_CACHE
    os.makedirs(cache, exist_ok=True)
    resultados, pendientes = {}, []
    for t in tareas:
        ruta = os.path.join(cache, "%02d_%02d_%s_%s.json" % (t[0], t[1], t[2], t[3]))
        if demo:
            resultados[t] = mapa_demo(t[1])
        elif os.path.exists(ruta):
            with open(ruta) as f:
                resultados[t] = json.load(f)
        else:
            pendientes.append((t, ruta))
    if pendientes:
        trabajadores = max(1, (os.cpu_count() or 2) - 1)
        print("Calculando %d mapas con %d procesos..." % (len(pendientes), trabajadores))
        with ProcessPoolExecutor(max_workers=trabajadores) as pool:
            for (t, ruta), res in zip(pendientes, pool.map(calcular_mapa, [p[0] for p in pendientes])):
                resultados[t] = res
                with open(ruta, "w") as f:
                    json.dump(res, f)
    return resultados


# =================================================================
# 4. DIBUJO
# =================================================================
def a_matriz(puntos):
    """Pasa la lista (lat, lon, rel) a una rejilla regular."""
    import numpy as np
    lats = sorted({p[0] for p in puntos})
    lons = sorted({p[1] for p in puntos})
    z = np.full((len(lats), len(lons)), np.nan)
    ilat = {v: i for i, v in enumerate(lats)}
    ilon = {v: i for i, v in enumerate(lons)}
    for la, lo, rel in puntos:
        z[ilat[la], ilon[lo]] = rel
    return np.array(lons), np.array(lats), z


def dibujar(puntos, instante, ssn, flujo, titulo_extra=""):
    """Un fotograma: devuelve una imagen PIL."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import BoundaryNorm, ListedColormap
    from PIL import Image
    try:
        import cartopy.crs as ccrs
        import cartopy.feature as cfeature
        proyeccion = ccrs.PlateCarree()
        ejes = {"projection": proyeccion}
    except Exception:
        ccrs = None
        ejes = {}
    fig = plt.figure(figsize=(10, 5.3), dpi=ANCHO_PX / 10)
    ax = fig.add_axes([0.01, 0.02, 0.86, 0.86], **ejes)
    lons, lats, z = a_matriz(puntos)
    cmap = ListedColormap(COLORES_REL)
    norma = BoundaryNorm(list(range(0, 101, 10)), cmap.N)
    opciones = {"transform": ccrs.PlateCarree()} if ccrs else {}
    malla = ax.pcolormesh(lons, lats, z, cmap=cmap, norm=norma, shading="nearest", alpha=0.85, **opciones)
    if ccrs:
        ax.set_global()
        ax.add_feature(cfeature.LAND, facecolor="#e5e7eb", zorder=0)
        ax.add_feature(cfeature.COASTLINE, linewidth=0.5, zorder=3)
        ax.add_feature(cfeature.BORDERS, linewidth=0.3, edgecolor="#555555", zorder=3)
        try:  # sombra de noche, si la versión de cartopy la incluye
            from cartopy.feature.nightshade import Nightshade
            ax.add_feature(Nightshade(instante, alpha=0.25), zorder=4)
        except Exception:
            pass
        ax.gridlines(draw_labels=False, linewidth=0.3, alpha=0.5, color="#888888")
        ax.plot(QTH_LON, QTH_LAT, marker="*", color="k", markersize=9, transform=ccrs.PlateCarree(), zorder=6)
    else:  # sin cartopy: rejilla sencilla y sin costas (solo para pruebas)
        ax.set_xlim(-180, 180)
        ax.set_ylim(-90, 90)
        ax.grid(True, linewidth=0.3)
        ax.plot(QTH_LON, QTH_LAT, marker="*", color="k", markersize=9)
    cb = fig.colorbar(malla, ax=ax, fraction=0.025, pad=0.01, ticks=list(range(0, 101, 20)))
    cb.ax.set_yticklabels(["%d%%" % v for v in range(0, 101, 20)], fontsize=7)
    sol = "SSN %.0f" % ssn + (" (flujo %.0f)" % flujo if flujo else "")
    fig.suptitle("%s · 11 m (%.1f MHz) · fiabilidad REL · %s %d W\n%s UTC · %s %s" % (
        QTH_LOC, FRECUENCIA_MHZ, MODO, POTENCIA_W, instante.strftime("%d/%m %H:00"), sol, titulo_extra),
        fontsize=9, y=0.995)
    fig.text(0.01, 0.005, "Modelo VOACAP (DVOACAP). Predicción de condiciones medias ajustada al flujo solar; "
             "no incluye Es esporádica ni tormentas geomagnéticas.", fontsize=6, color="#444444")
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return Image.open(buf).convert("RGB")


# =================================================================
# 5. GIF
# =================================================================
def crear_gif(imagenes, ruta):
    paleta = [im.convert("P", palette=1, colors=128) for im in imagenes]  # 128 colores: GIF mucho más ligero
    paleta[0].save(ruta, save_all=True, append_images=paleta[1:], duration=MS_POR_FOTOGRAMA,
                   loop=0, optimize=True)
    print("GIF creado: %s (%.1f MB)" % (ruta, os.path.getsize(ruta) / 1e6))


# =================================================================
# 6. PÁGINA WEB
# =================================================================
def escribir_pagina(instantes):
    """Página sencilla con el GIF, por si quieres enlazarla en lugar de incrustar la imagen."""
    html = """<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Propagación 11 m · %s</title>
<style>body{margin:0;font-family:system-ui,sans-serif;background:#929394;color:#1f2937;text-align:center}
img{max-width:100%%;height:auto;background:#fff}p{margin:8px}</style></head><body>
<p><strong>Previsión de propagación en 11 m desde %s</strong></p>
<img src="%s" alt="Previsión VOACAP 11 m">
<p>Actualizado: %s UTC · válido desde %s hasta %s UTC</p></body></html>""" % (
        QTH_LOC, QTH_LOC, NOMBRE_GIF,
        dt.datetime.now(dt.timezone.utc).strftime("%d/%m/%Y %H:%M"),
        instantes[0].strftime("%d/%m %H:00"), instantes[-1].strftime("%d/%m %H:00"))
    with open(os.path.join(CARPETA_SALIDA, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)


# =================================================================
# 7. PROGRAMA PRINCIPAL
# =================================================================
def main():
    ap = argparse.ArgumentParser(description="GIF diario de propagación VOACAP en 11 m")
    ap.add_argument("--demo", action="store_true", help="datos falsos, solo prueba el dibujo y el GIF")
    ap.add_argument("--validar", action="store_true", help="un mapa con los datos de la imagen de voacap.com (oct, SSN 53, 15 UT)")
    ap.add_argument("--rapido", action="store_true", help="3 horas en resolución gruesa")
    a = ap.parse_args()
    os.makedirs(CARPETA_SALIDA, exist_ok=True)

    if a.validar:  # mismos datos que la imagen 2026_Oct_27.100_REL_15UT.jpg para comparar a ojo
        t = (10, 15, 53.0, RESOLUCION)
        res = calcular_todos([t], False)
        im = dibujar(res[t], dt.datetime(2026, 10, 27, 15), 53.0, None, "· VALIDACIÓN")
        ruta = os.path.join(CARPETA_SALIDA, "validacion_oct_15UT.png")
        im.save(ruta)
        print("Compara %s con la imagen de voacap.com." % ruta)
        return

    ahora = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(minutes=30)
    inicio = ahora.replace(minute=0, second=0, microsecond=0)
    n = 3 if a.rapido else HORAS_ADELANTE
    resol = "coarse" if a.rapido else RESOLUCION
    instantes = [inicio + dt.timedelta(hours=i) for i in range(n)]
    previsto = {} if a.demo else leer_prevision_flujo()
    datos_dia = {i.date(): ssn_del_dia(i.date(), previsto) for i in instantes}
    tareas = [(i.month, i.hour, datos_dia[i.date()][0], resol) for i in instantes]

    resultados = calcular_todos(sorted(set(tareas)), a.demo)
    imagenes = []
    for i, t in zip(instantes, tareas):
        ssn, flujo = datos_dia[i.date()]
        imagenes.append(dibujar(resultados[t], i, ssn, flujo, "· previsión"))
    ruta = os.path.join(CARPETA_SALIDA, NOMBRE_GIF)
    crear_gif(imagenes, ruta)
    escribir_pagina(instantes)


if __name__ == "__main__":
    main()
