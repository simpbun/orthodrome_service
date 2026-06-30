from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from pyproj import Geod, Transformer
import re

app = Flask(__name__)
CORS(app)

def parse_wkt_point(wkt_str):
    """
    Извлекает lon, lat из строки формата POINT(lon lat) или POINT(lon, lat).
    Возвращает кортеж (lon, lat) в виде float.
    """
    # Удаляем 'POINT' и скобки, оставляем содержимое
    match = re.search(r'POINT\s*\(\s*([-\d.]+)[,\s]+([-\d.]+)\s*\)', wkt_str, re.IGNORECASE)
    if not match:
        raise ValueError(f'Неверный формат WKT: {wkt_str}')
    lon, lat = float(match.group(1)), float(match.group(2))
    return lon, lat

def orthodrome_line(lon1, lat1, lon2, lat2, total_points):
    """
    Возвращает список точек [lon, lat] ортодромии на эллипсоиде WGS84.
    total_points - общее количество точек в линии (начало + промежуточные + конец).
    """
    if total_points < 2:
        raise ValueError('Количество точек должно быть >= 2')
    geod = Geod(ellps='WGS84')
    # Число промежуточных точек (не считая начальную и конечную)
    npts = total_points - 2
    if npts > 0:
        extra = geod.npts(lon1, lat1, lon2, lat2, npts)
    else:
        extra = []
    line = [(lon1, lat1)] + extra + [(lon2, lat2)]
    return [[lon, lat] for lon, lat in line]

@app.route('/api/orthodrome', methods=['POST'])
def orthodrome():
    data = request.json
    # Проверка наличия всех полей
    required = ['point1', 'point2', 'cs', 'count']
    for field in required:
        if field not in data:
            return jsonify({'error': f'Отсутствует поле {field}'}), 400

    try:
        # Парсим точки
        lon1_in, lat1_in = parse_wkt_point(data['point1'])
        lon2_in, lat2_in = parse_wkt_point(data['point2'])
        cs_in = int(data['cs'])      # EPSG исходной системы координат
        count = int(data['count'])
        if count < 2:
            raise ValueError('count должен быть >= 2')
    except Exception as e:
        return jsonify({'error': f'Ошибка разбора входных данных: {str(e)}'}), 400

    # Если исходная СК не WGS84, трансформируем координаты
    if cs_in != 4326:
        try:
            # Создаём трансформер из заданной СК в WGS84 (4326)
            transformer = Transformer.from_crs(f'EPSG:{cs_in}', 'EPSG:4326', always_xy=True)
            lon1_wgs, lat1_wgs = transformer.transform(lon1_in, lat1_in)
            lon2_wgs, lat2_wgs = transformer.transform(lon2_in, lat2_in)
        except Exception as e:
            return jsonify({'error': f'Ошибка преобразования координат: {str(e)}'}), 400
    else:
        lon1_wgs, lat1_wgs = lon1_in, lat1_in
        lon2_wgs, lat2_wgs = lon2_in, lat2_in

    try:
        coords_wgs = orthodrome_line(lon1_wgs, lat1_wgs, lon2_wgs, lat2_wgs, count)
    except Exception as e:
        return jsonify({'error': f'Ошибка расчёта ортодромии: {str(e)}'}), 500

    # Формируем GeoJSON (LineString)
    geojson = {
        "type": "Feature",
        "geometry": {
            "type": "LineString",
            "coordinates": coords_wgs  # [ [lon, lat], ... ]
        },
        "properties": {}
    }
    return jsonify(geojson)

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

if __name__ == '__main__':
    app.run(debug=True, host='127.0.0.1', port=5000)