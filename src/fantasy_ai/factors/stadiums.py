"""Home stadium location and roof, by ESPN pro-team abbreviation. dome=True means a fixed or
retractable roof (weather ignored). International and neutral-site games are not handled."""

STADIUMS = {
    "ARI": (33.528, -112.263, True),  "ATL": (33.755, -84.401, True),  "BAL": (39.278, -76.623, False),
    "BUF": (42.774, -78.787, False),  "CAR": (35.226, -80.853, False), "CHI": (41.862, -87.617, False),
    "CIN": (39.095, -84.516, False),  "CLE": (41.506, -81.700, False), "DAL": (32.748, -97.093, True),
    "DEN": (39.744, -105.020, False), "DET": (42.340, -83.046, True),  "GB": (44.501, -88.062, False),
    "HOU": (29.685, -95.411, True),   "IND": (39.760, -86.164, True),  "JAX": (30.324, -81.637, False),
    "KC": (39.049, -94.484, False),   "LV": (36.091, -115.184, True),  "LAC": (33.953, -118.339, True),
    "LAR": (33.953, -118.339, True),  "MIA": (25.958, -80.239, False), "MIN": (44.974, -93.258, True),
    "NE": (42.091, -71.264, False),   "NO": (29.951, -90.081, True),   "NYG": (40.814, -74.074, False),
    "NYJ": (40.814, -74.074, False),  "PHI": (39.901, -75.168, False), "PIT": (40.447, -80.016, False),
    "SF": (37.403, -121.970, False),  "SEA": (47.595, -122.332, False), "TB": (27.976, -82.503, False),
    "TEN": (36.166, -86.771, False),  "WSH": (38.908, -76.864, False),
}
