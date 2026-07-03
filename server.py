from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from pyproj import Geod, Transformer
from shapely.geometry import LineString, Polygon
from shapely.ops import split, linemerge
import re

app = Flask(__name__)
CORS(app)

def parse_wkt_point(wkt_str):
    match = re.search(r'POINT\s*\(\s*([-\d.]+)[,\s]+([-\d.]+)\s*\)', wkt_str, re.IGNORECASE)
    if not match:
        raise ValueError(f'Неверный формат WKT: {wkt_str}')
    return float(match.group(1)), float(match.group(2))

def orthodrome_coords(lon1, lat1, lon2, lat2, total_points):
    if total_points < 2:
        raise ValueError('Количество точек должно быть >= 2')
    geod = Geod(ellps='WGS84')
    npts = total_points - 2
    extra = geod.npts(lon1, lat1, lon2, lat2, npts) if npts > 0 else []
    line = [(lon1, lat1)] + extra + [(lon2, lat2)]
    return [[lon, lat] for lon, lat in line]

def parse_ewkt_polygon(ewkt_str):
    match = re.match(r'SRID=(\d+);\s*POLYGON\s*\(\((.*)\)\)', ewkt_str, re.IGNORECASE)
    if not match:
        raise ValueError(f'Неверный формат EWKT: {ewkt_str}')
    srid = int(match.group(1))
    coords_text = match.group(2)
    pairs = re.findall(r'([-\d.]+)\s+([-\d.]+)', coords_text)
    if len(pairs) < 3:
        raise ValueError('Полигон должен иметь минимум 3 точки')
    polygon = Polygon([(float(lon), float(lat)) for lon, lat in pairs])
    return srid, polygon

def split_line_by_zones(line_coords, zones_ewkt):
    """
    Разбивает ортодромию на отрезки с точным обрезанием по границам зон.
    Возвращает (segments, outline_polygons)
    """
    line = LineString(line_coords)
    zone_polygons = []
    for ewkt in zones_ewkt:
        try:
            srid, polygon = parse_ewkt_polygon(ewkt)
            if srid != 4326:
                transformer = Transformer.from_crs(f'EPSG:{srid}', 'EPSG:4326', always_xy=True)
                polygon = Polygon([transformer.transform(x, y) for x, y in polygon.exterior.coords])
            zone_polygons.append(polygon)
        except Exception as e:
            print(f"Ошибка разбора зоны: {e}")
            continue

    if not zone_polygons:
        return [{'coords': line_coords, 'color': 'blue'}], []

    all_zones = zone_polygons[0]
    for z in zone_polygons[1:]:
        all_zones = all_zones.union(z)

    # Получаем части линии внутри зон и снаружи
    inside = line.intersection(all_zones)   # MultiLineString или LineString
    outside = line.difference(all_zones)    # MultiLineString или LineString

    # Приводим к списку LineString
    def to_lines(geom):
        if geom.is_empty:
            return []
        if geom.geom_type == 'LineString':
            return [geom]
        elif geom.geom_type == 'MultiLineString':
            return list(geom.geoms)
        else:
            return []

    inside_lines = to_lines(inside)
    outside_lines = to_lines(outside)

    # Собираем все куски с пометкой "тип" и расстоянием от начала исходной линии
    pieces = []
    for ls in inside_lines:
        dist = line.project(ls.interpolate(0.5, normalized=False))
        pieces.append({'geom': ls, 'type': 'inside', 'dist': dist})
    for ls in outside_lines:
        dist = line.project(ls.interpolate(0.5, normalized=False))
        pieces.append({'geom': ls, 'type': 'outside', 'dist': dist})

    # Сортируем вдоль линии
    pieces.sort(key=lambda p: p['dist'])

    # Теперь назначаем цвета:
    # inside -> red
    # outside -> blue, но если между двумя red, то green
    segments = []
    for piece in pieces:
        coords = [list(c) for c in piece['geom'].coords]
        color = 'red' if piece['type'] == 'inside' else 'blue'
        segments.append({'coords': coords, 'color': color})

    # Находим индексы красных сегментов
    red_indices = [i for i, s in enumerate(segments) if s['color'] == 'red']
    if len(red_indices) >= 2:
        # Между соседними красными блоками делаем синие сегменты зелёными
        for i in range(len(red_indices) - 1):
            left = red_indices[i]
            right = red_indices[i+1]
            for j in range(left + 1, right):
                if segments[j]['color'] == 'blue':
                    segments[j]['color'] = 'green'

    # Шаг 5: меньшие части зон для облётного контура
    outline_polygons = []
    for polygon in zone_polygons:
        inter = line.intersection(polygon)
        if inter.is_empty or inter.geom_type not in ('LineString', 'MultiLineString'):
            continue
        try:
            parts = split(polygon, line)
            polys = [g for g in parts.geoms if g.geom_type == 'Polygon']
            if len(polys) >= 2:
                smaller = min(polys, key=lambda p: p.area)
                coords = [[x, y] for x, y in smaller.exterior.coords]
                outline_polygons.append(coords)
        except Exception as e:
            print(f"Не удалось разделить зону: {e}")

    return segments, outline_polygons

@app.route('/api/orthodrome', methods=['POST'])
def orthodrome():
    data = request.json
    required = ['point1', 'point2', 'cs', 'count']
    for field in required:
        if field not in data:
            return jsonify({'error': f'Отсутствует поле {field}'}), 400

    try:
        lon1_in, lat1_in = parse_wkt_point(data['point1'])
        lon2_in, lat2_in = parse_wkt_point(data['point2'])
        cs_in = int(data['cs'])
        count = int(data['count'])
        if count < 2:
            raise ValueError('count должен быть >= 2')
    except Exception as e:
        return jsonify({'error': f'Ошибка разбора: {str(e)}'}), 400

    if cs_in != 4326:
        try:
            transformer = Transformer.from_crs(f'EPSG:{cs_in}', 'EPSG:4326', always_xy=True)
            lon1_wgs, lat1_wgs = transformer.transform(lon1_in, lat1_in)
            lon2_wgs, lat2_wgs = transformer.transform(lon2_in, lat2_in)
        except Exception as e:
            return jsonify({'error': f'Ошибка преобразования: {str(e)}'}), 400
    else:
        lon1_wgs, lat1_wgs = lon1_in, lat1_in
        lon2_wgs, lat2_wgs = lon2_in, lat2_in

    try:
        line_coords = orthodrome_coords(lon1_wgs, lat1_wgs, lon2_wgs, lat2_wgs, count)
    except Exception as e:
        return jsonify({'error': f'Ошибка расчёта: {str(e)}'}), 500

    zones = data.get('zones', [])
    if zones:
        segments, outlines = split_line_by_zones(line_coords, zones)
        features = []
        for seg in segments:
            features.append({
                'type': 'Feature',
                'geometry': {'type': 'LineString', 'coordinates': seg['coords']},
                'properties': {'color': seg['color']}
            })
        response = {
            'type': 'FeatureCollection',
            'features': features
        }
        if outlines:
            response['outlines'] = outlines
        return jsonify(response)
    else:
        geojson = {
            'type': 'Feature',
            'geometry': {'type': 'LineString', 'coordinates': line_coords},
            'properties': {}
        }
        return jsonify(geojson)

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

if __name__ == '__main__':
    app.run(debug=True, host='127.0.0.1', port=5000)