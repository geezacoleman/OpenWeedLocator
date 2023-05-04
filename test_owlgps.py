from OwlGPS import OwlGPS
import time
import threading
import serial
import io

if __name__ == "__main__":
	gps = OwlGPS()

	upd_routine = threading.Thread(target = gps.update)
	upd_routine.start()
	try:
		count = 0
		while True:
			print(f"\n\n####### GPS UPDATE COUNT : {count} #########")
			print(f"Fixed: {gps.fixed}")
			print(f"Num Sattelites: {gps.num_sats}")
			print(f"Time: {gps.last_alive}")
			print(f"Latitude: {gps.latitude}")
			print(f"Longitude: {gps.longitude}")
			print(f"Altitude: {gps.altitude}")
			print(f"Speed: {gps.speed_knots} knots, {gps.speed_mps} mps")
			print(f"###############################################")
			count+=1
			time.sleep(3)
	except Exception as e:
		print(e)
		upd_routine.join()
		print('\nexiting')
		
		
	
