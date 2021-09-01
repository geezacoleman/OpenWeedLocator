import pynmea2

# based on the code/article here: https://ozzmaker.com/using-python-with-a-gps-receiver-on-a-raspberry-pi/
# example NMEA string from JD (RMC enabled):
# $GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A
#  TYPE, timestamp, status (A = active/V = void), LAT (DD,heading), LONG (DD,heading), SPEED (kn), track angle, date, mag variation, checksum

def read_gps_speed(nmeaRaw):
    # check if incoming NMEA is a list of different types (RMC, GGA etc) or if just one type
    if type(nmeaRaw) == list:
        for i, gpsString in enumerate(nmeaRaw):
            if 'RMC' in gpsString:
                nmeaString = nmeaRaw[i]
            else:
                return 0  # returns 0 speed if RMC not present

    elif type(nmeaRaw) == str:
        nmeaString = nmeaRaw

    else:
        return 0  # returns 0 speed for max delay if GPS signal lost

    nmeaMessage = pynmea2.parse(nmeaString)
    speed_ms = nmeaMessage.spd_over_grnd * 0.51444 # output from RMC is speed in knots

    return speed_ms
