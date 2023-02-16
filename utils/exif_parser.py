import piexif
import csv
import folium

def plot_gps_detections(gps_points):
    # Create a map centered at the first GPS point
    map = folium.Map(location=[gps_points[0][0], gps_points[0][1]])

    # Add markers for each GPS point
    for lat, lon, altitude, gps_id in gps_points:
        folium.Marker(
            location=[lat, lon],
            popup=gps_id
        ).add_to(map)

    # Show the map
    map.show()



def add_gps_exif(image_array, ):
    pass



def get_gps_from_exif(image_path):
    exif_data = piexif.load(image_path)
    gps_points = []

    latitude_keys = [key for key in exif_data["GPS"] if "GPSLatitude" in key]
    longitude_keys = [key for key in exif_data["GPS"] if "GPSLongitude" in key]

    for i, latitude_key in enumerate(latitude_keys):
        longitude_key = longitude_keys[i]

        latitude_ref_key = f"GPS.GPSLatitudeRef" if i == 0 else f"GPS.GPSLatitudeRef_{i}"
        altitude_ref_key = f"GPS.GPSAltitudeRef_{i}"
        altitude_key = f"GPS.GPSAltitude_{i}"
        id_key = f"GPS.GPSID_{i}"

        latitude_ref = exif_data["GPS"].get(latitude_ref_key, b'N')
        longitude_ref = exif_data["GPS"][f"GPS.GPSLongitudeRef_{i}"]
        altitude_ref = exif_data["GPS"].get(altitude_ref_key, 1)
        latitude = exif_data["GPS"][latitude_key]
        longitude = exif_data["GPS"][longitude_key]
        altitude = exif_data["GPS"].get(altitude_key, 0)
        gps_id = exif_data["GPS"].get(id_key, '')

        latitude = latitude[0][0] / latitude[0][1] + latitude[1][0] / latitude[1][1] / 60 + latitude[2][0] / latitude[2][1] / 3600
        longitude = longitude[0][0] / longitude[0][1] + longitude[1][0] / longitude[1][1] / 60 + longitude[2][0] / longitude[2][1] / 3600
        altitude = altitude[0] / altitude[1]

        if latitude_ref == b'S':
            latitude = -latitude
        if longitude_ref == b'W':
            longitude = -longitude
        if altitude_ref == 1:
            altitude = -altitude

        gps_points.append((latitude, longitude, altitude, gps_id))

    return gps_points



