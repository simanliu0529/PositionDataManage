"""Shared spatial validation and conservative radius prefilter for batch nodes."""
import math
from urllib.parse import parse_qs
from backend.control import APIError
from cleaning.clean import normalize

EARTH_RADIUS = 6371008.8
BBOX = {'west','east','south','north'}
RADIUS = {'center_lon','center_lat','radius_m'}

def distance(lon1, lat1, lon2, lat2):
    dlat, dlon = math.radians(lat2-lat1), math.radians(lon2-lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1))*math.cos(math.radians(lat2))*math.sin(dlon/2)**2
    return 2*EARTH_RADIUS*math.asin(math.sqrt(max(0,min(1,a))))

def params(query, extra=()):
    values = parse_qs(query, keep_blank_values=True)
    allowed = {'batch_id','user_id','location_id','start','end','limit','offset'} | BBOX | RADIUS | set(extra)
    if any(key not in allowed or len(value)!=1 for key,value in values.items()):
        raise APIError(400,'INVALID_PARAMETERS','参数未知或重复。')
    p = {key:value[0] for key,value in values.items() if value[0] != ''}
    try:
        limit, offset = int(p.pop('limit',50)), int(p.pop('offset',0))
        if not 1<=limit<=200 or not 0<=offset<=10000:
            raise ValueError
        for key in ('start','end'):
            if key in p: p[key] = normalize(('0',p[key],'0','0','0'))[1]
        if 'start' in p and 'end' in p and p['start']>=p['end']: raise ValueError
        supplied = set(p)
        if (supplied&BBOX and not BBOX<=supplied) or (supplied&RADIUS and not RADIUS<=supplied) or (supplied&BBOX and supplied&RADIUS):
            raise ValueError
        for key in BBOX | RADIUS:
            if key in p:
                p[key]=float(p[key])
                bound=50000 if key=='radius_m' else 180 if key in ('west','east','center_lon') else 90
                if not math.isfinite(p[key]) or not -bound<=p[key]<=bound: raise ValueError
        if 'radius_m' in p and not 1<=p['radius_m']<=50000: raise ValueError
        if 'west' in p and (p['west']>=p['east'] or p['south']>=p['north']): raise ValueError
    except (ValueError, OverflowError):
        raise APIError(400,'INVALID_SPATIAL_FILTER','请检查时区时间、矩形四界、中心与半径及分页范围。') from None
    return p, limit, offset

def where_clause(p):
    terms, values = [], []
    for name, column, operator in [('user_id','user_id','='),('location_id','location_id','='),('start','checkin_time','>='),('end','checkin_time','<'),('west','longitude','>='),('east','longitude','<='),('south','latitude','>='),('north','latitude','<=')]:
        if name in p:
            terms.append(f'{column} {operator} ?'); values.append(p[name])
    if 'radius_m' in p:
        lon,lat,radius=p['center_lon'],p['center_lat'],p['radius_m']
        angular=radius/EARTH_RADIUS
        south,north=max(-90,lat-math.degrees(angular)),min(90,lat+math.degrees(angular))
        terms += ['latitude >= ?', 'latitude <= ?'];values += [south,north]
        if south>-90 and north<90:
            delta=math.degrees(math.asin(min(1,math.sin(angular)/math.cos(math.radians(lat)))))
            if lon-delta>=-180 and lon+delta<=180:
                terms += ['longitude >= ?', 'longitude <= ?'];values += [lon-delta,lon+delta]
        terms.append('haversine(longitude,latitude,?,?) <= ?')
        values += [lon,lat,radius+1e-7]
    return ' AND '.join(terms) or '1=1', values
