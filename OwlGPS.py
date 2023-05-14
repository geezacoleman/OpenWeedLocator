import pynmea2
import serial
import io
import time
import datetime

class OwlGPS():
	'''
	OWLGPS utilizes serial and pynmea2 to read back GPS values from a UART
	NMEA stream into a centralized location. All the values read back from via
	the object properties. 

	TODO: FIGURE OUT A CLEAN CLASS METHOD FOR WRITING DATA TO EXIF METADATA
	'''
	def __init__(self, port = '/dev/ttyS0', baud = 9600):
		self.ser = serial.Serial('/dev/ttyS0', 9600, timeout=1)
		self.sio = io.TextIOWrapper(io.BufferedRWPair(self.ser, self.ser))
		
		self._fixed = False
		self._latitude = None
		self._latitude_direction = None
		self._longitude = None
		self._longitude_direction = None
		self._altitude = None
		self._altitude_units = None
		self._speed_knots = None
		self._speed_mps = None
		self._num_sats = None
		self._last_alive = None		
		
	def update(self):
    	'''
    	update uses the serial port defined in the constructor to update the GPS's attributes with 
    	current data. update is designed to run in a thread while the gps 
    	attributes are read externally. 
    	update will optimistically continue in case of SerialException or ParseError.
    	All other exceptions will cause a return
    	'''
		while True:
			try:
				# Read the lines from the SPI
				line = self.sio.readline()
							
				msg = pynmea2.parse(line)
				
				#print(line)
				if 'GPGGA' in line:   # Fields contained in GGA lines
					self.altitude = msg.altitude
					self.latitude_direction = msg.lat_dir
					self.latitude = msg.lat
					self.longitude_direction = msg.lon_dir
					self.longitude = msg.lon
					self.num_sats = msg.num_sats
					self.last_alive = msg.timestamp
					self.altitude_units = msg.altitude_units

				elif 'GPRMC' in line:    # Fields contained in RMC lines 
					self.fixed = msg.status
					self.last_alive = msg.timestamp
					self.latitude_direction = msg.lat_dir
					self.latitude = msg.lat
					self.longitude_direction = msg.lon_dir
					self.longitude = msg.lon
					self.speed_knots = msg.spd_over_grnd
					
				#print(self.latitude)
				#print(self.longitude)
				#print(self.fixed)

			except serial.SerialException as e:
				print(f"Device error: {e}")
				continue
			except pynmea2.ParseError as e:
				print(f"Parse error: {e}")
				self.fixed = False
				continue
			except KeyboardInterrupt:
				print('Break from keyboard')
				return
			except Exception as e:
				print(f"Unknown exception {e}. Terminating GPS Update loop...")
				return
	
	@property
	def fixed(self):
		return self._fixed
		
	@fixed.setter 
	def fixed(self, value):
		
		if isinstance(value, bool):
			self._fixed = value
		elif isinstance(value, str):
			if value == "V":
				self._fixed = False
			elif value == "A":
				self._fixed = True
			else:
				self._fixed = None
		else:
			self._fixed = None
		
	@property
	def latitude_direction(self):
		return self._latitude_direction
		
	@latitude_direction.setter 
	def latitude_direction(self, value):
		
		if isinstance(value, str):
			self._latitude_direction = value
		else:
			self._latitude_direction = None

	@property
	def latitude(self):
		return self._latitude
		
	@latitude.setter 
	def latitude(self, value):
		
		if isinstance(value, float):
			self._latitude = value
		elif isinstance(value, str):
			try:
				self._latitude = float(value)
			except ValueError:
				self._latitude = None
		else:
			self._latitude = None
	
	@property
	def longitude_direction(self):
		return self._longitude_direction
		
	@longitude_direction.setter 
	def longitude_direction(self, value):
		
		if isinstance(value, str):
			self._longitude_direction = value
		else:
			self._longitude_direction = None

	@property
	def longitude(self):
		return self._longitude
		
	@longitude.setter 
	def longitude(self, value):
		
		if isinstance(value, float):
			self._longitude = value
		elif isinstance(value, str):
			try:
				self._longitude = float(value)
			except ValueError:
				self._longitude = None
		else:
			self._longitude = None
			
	@property
	def altitude(self):
		return self._altitude
		
	@altitude.setter 
	def altitude(self, value):
		
		if isinstance(value, float):
			self._altitude = value
		elif isinstance(value, str):
			try:
				self._altitude = float(value)
			except ValueError:
				self._altitude = None
		else:
			self._altitude = None
	
	@property
	def altitude_units(self):
		return self._altitude_units
		
	@altitude_units.setter 
	def altitude_units(self, value):
		
		if isinstance(value, str):
			self._altitude_units = value
		else:
			self._altitude_units = None		

	@property
	def speed_knots(self):
		return self._speed_knots
		
	@speed_knots.setter 
	def speed_knots(self, value):
		
		if isinstance(value, float):
			self._speed_knots = value
		elif isinstance(value, str):
			try:
				self._speed_knots = float(value)
			except ValueError:
				self._speed_knots = None
		else:
			self._speed_knots = None
			
	@property
	def speed_mps(self):
		if self._speed_knots is None:
			return None
		else:
			return self._speed_knots * 0.514444
			
	@property
	def num_sats(self):
		return self._num_sats
		
	@num_sats.setter 
	def num_sats(self, value):
		
		if isinstance(value, float) or isinstance(value, int):
			self._num_sats = value
		elif isinstance(value, str):
			try:
				self._num_sats = int(value)
			except ValueError:
				self._num_sats = None
		else:
			self._num_sats = None
			
	@property
	def last_alive(self):
		return self._last_alive
		
	@last_alive.setter 
	def last_alive(self, value):
		
		if isinstance(value, datetime.time):
			self._last_alive = value
		elif isinstance(value, str):
			try:
				self._last_alive = datetime.time(value)
			except ValueError:
				self._last_alive = None
		else:
			self._last_alive = None
	
