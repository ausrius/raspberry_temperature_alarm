#!/usr/bin/python

import sys, getopt
import time, datetime
import RPi.GPIO as GPIO
import threading
import peewee # a nice object wrapper around the MySQL tables
from smbus2 import SMBus # the SMBus (i.e. i2c channel) will read the temp sensor via the ADC

import logging
import logging.handlers

# ========================= DEFAULT VALUES (start) ===================================
# MODE can be one of the three:
#   "SENSOR" - measures temperature and stores in the DB
#   "CONTROLLER" - reads the DB and raises an alarm if something is wrong
#   "BOTH" - acts as a sensor and as a controller at the same time
MODE = 'both'

# GENERAL settings
DB_NAME = 'temperature'
DB_HOST = None # THIS MUST BE SUPPLIED VIA THE COMMAND LINE
DB_USER = 'pi'
DB_PWD  = 'lame'

# SENSOR settings
SENSOR_PLACE = "Raspberry Fields" # arbitrary description of the location
FREQ_SENSOR = 60 # [seconds] how often does the sensor measure the temperature
SENSOR_ADDR = 0x48 # that's what the PCF8591 YL-40 AD DA is wired to
SENSOR_CHANNEL = 1 # the i2c device on Raspberry - the kernel decided it's number 1
SENSOR_INPUT = 2 # the ADC has four inputs (AIN0 - AIN3), by default all of them are wired to its internal sensors except AIN2
# and so we use AIN2 to connect the temperature sensor LM35DZ
temperature_bus = SMBus(SENSOR_CHANNEL)

# CONTROLLER settings
MAX_TEMP = 35.5 # We raise an alarm above this value
# If it gets too hot or if we have some other problem (such as a connectivity problem to read the temperature)
# then we flash some LEDs and make some noise using a piezo-buzzer.
# We are nice and we only make the noise once in a while, but we flash the lights all the time while the alarm flag is raised
NOISE_DURATION = 10 # how long does the alarm sound, in seconds
DELAY_BETWEEN_NOISES = 10*60 # delay between buzzer sounds, in seconds
FREQ_CONTROL = 120 # [seconds] how often does the controller query the DB to check the temperature
FREQ_ALARM = 2 # how fast is the alarm sequence looping - basically equals to the delay of the alarm start/stop

# GPIO settings
RED = 16 # pin of the red LED; GPIO.BOARD notation
PIN_BUZZER = 11

# ========================= DEFAULT VALUES (end) =====================================

def usage():
	print "Usage: %s -h host [-m mode] [-s] [-c] [-d dbname] [-u username] ..." % sys.argv[0]
	print " Reads temperature and rings an alarm if it gets too hot (or if we fail to connect to the DB on the host)."
	print " -m=mode: where mode is one of the three: SENSOR, CONTROLLER, or BOTH"
	print "          Sensor mode only reads the temperature sensor and sends the data to a MySQL DB."
	print "          Controller reads the values from the DB and makes noises if it's too hot."
	print "          Both is both at the same time (DEFAULT = %s)." % MODE
	print " -d dbname: DEFAULT = %s" % DB_NAME
	print " -u username: DEFAULT = %s" % DB_USER
	print " -p password: DEFAULT = %s" % DB_PWD
	print " -L location_name: description of the sensor location (DEFAULT = %s)" % SENSOR_PLACE
	print " -a seconds: how aften does the sensor measure the temperature (DEFAULT = %s), Min 10 secs." % FREQ_SENSOR
	print " -b seconds: how often does the controller query the DB to check the temperature (DEFAULT = %s), Min 30 secs." % FREQ_CONTROL
	print " -t celsius: max temperature. The controller raises an alarm above this value (DEFAULT = %s)" % MAX_TEMP
	print " -n seconds: noise duration during the alarm (DEFAULT = %s), between 1 and 60." % NOISE_DURATION
	print " -r seconds: rest period, delay between the STARTING moments of consecutive noises during an alarm (DEFAULT = %s)" % DELAY_BETWEEN_NOISES
	print " -c: lower screen log level to DEBUG (DEFAULT level is INFO)"
	print " -s: lower syslog level to INFO (DEFAULT level is WARNING)"
	print " AN EXAMPLE:"
	print " %s -h 192.168.0.11 -c -u monika -n 20 -r 20" % sys.argv[0]
	print "   This would work both as a sensor and a controller, print lots of debugging info to the screen, "
	print "   connect to the '%s' database using user name 'monika' and would beep the buzzer almost continuously " % DB_NAME
	print "   if an alarm is raised because the delay interval between the starts of the beeps equals to the duration of the beep."


db=None # the database
logger = None # to be set up later

def init_gpio():
	GPIO.setmode(GPIO.BOARD)
	GPIO.setwarnings(False)
	GPIO.setup(RED, GPIO.OUT)
	GPIO.setup(PIN_BUZZER, GPIO.OUT, initial=GPIO.LOW)
	val = temperature_bus.read_byte_data(SENSOR_ADDR, SENSOR_INPUT) # the first byte is always some nonsense

def destroy(stop_threads, evW, evR):
	stop_threads.set()
	evW.clear()
	evR.clear()
	db.close()
	GPIO.cleanup()
	temperature_bus.close()

class Tvalue(peewee.Model):
	""" this is where we store temperature values - the table gets auto created in MySql DB """
	tvalueid = peewee.PrimaryKeyField()
	tvalue=peewee.FloatField()
	tplace=peewee.TextField()
	twhen=peewee.DateTimeField() #default=datetime.datetime.now)

	class Meta:
		database=db

def alarm_flag(ev, ysn_problem):
	""" raises or clears the alarm flag depending on the problem status """
	if ev.isSet():
		if not ysn_problem: ev.clear()
	else:
		if ysn_problem: ev.set()

def io_flash_lights(ev):
	""" activate some LED on the GPIO """
	flash_time = 0.5
	logger.debug('Entering')
	try:
		while ev.isSet():
			logger.debug("....flashing RED...... flashing RED ......")
			GPIO.output(RED, GPIO.HIGH)
			time.sleep(flash_time)
			GPIO.output(RED, GPIO.LOW)
			time.sleep(flash_time)
	except RuntimeError, err:
		logger.error('Cannot flash the LED: %s' % err)
	logger.debug('Exiting')

def io_activate_buzzer(ev, how_long):
	""" make some annoying noises """
	logger.debug('Entering')
	timeStart = time.time()
	while ev.isSet() and ((time.time() - timeStart) < how_long):
		GPIO.output(PIN_BUZZER, GPIO.HIGH)
		time.sleep(.25)
		logger.debug("....eeeee oooooo eeeeee oooooooo EEEEEEE oooooo eeeeee OOOOOO ......")
		GPIO.output(PIN_BUZZER, GPIO.LOW)
		time.sleep(.75)
	ev.clear()
	logger.debug('Exiting')

def io_temp_sensor():
	""" reads the temperature from the connected sensor, returns degrees Celsius """
	temperature_bus.read_byte_data(SENSOR_ADDR, SENSOR_INPUT)
	# discard the first one as it is likely a wrong one (that's how the ADC works)
	time.sleep(0.1)
	val = temperature_bus.read_byte_data(SENSOR_ADDR, SENSOR_INPUT)
	# we get val==0 for 0 degrees Celsius or less, as the sensor gives out 0 Volts or a negative number which the ADC ignores
	# max value from the ADC is 255 and it happens whenever the sensor outputs 3,3V, which is equivalent to 330 degress (never happens)
	celsius = float(val)*100.*3.3/255.
	# the smallest change in the read-out is therefore 100*3.3/255 = 1.29 degrees, and this limits our accuracy to >1 degree
	# therefore not to fool anyone we cut the digits after the decimal point:
	return int(celsius)

def run_gather_temp(freq, stop_threads, ev):
	""" An independent thread that reads the temperature sensor and stores the value in a remote DB """
	logger.debug('Entering')
	while not stop_threads.isSet():
		new_temp = io_temp_sensor()
		logger.info('Measured temp: %.2f' % new_temp)
		new_tvalue = Tvalue(tvalue=new_temp, tplace = SENSOR_PLACE)
		try:
			if ev.isSet(): # try to reconnect
				db.close()
				db.connect()
			new_tvalue.save()
			if ev.isSet():
				ev.clear()
		except peewee.OperationalError, err:
			# ALARM
			ev.set()
			logger.debug("CANNOT SAVE TO THE DB [%s]", err)
		time.sleep(freq)
	logger.debug('Exiting')

def run_checkdb(freq, stop_threads, ev):
	""" Another independent thread: it just reads the DB to see what temp we have.
	If it's over the limit or there's no connection, raises the Alarm flag (ev gets set) """
	logger.debug('Entering')
	cnt = 0
	problem_db = False
	problem_heat = False
	while not stop_threads.isSet():
		cnt += 1
		row = None
		try:
			if problem_db: # try to reconnect
				db.close()
				db.connect()
				problem_db = False
			# get the last measurement recorded in SENSOR_PLACE
			rows = Tvalue.select().where(Tvalue.tplace==SENSOR_PLACE).order_by(Tvalue.twhen.desc()).limit(1)
			if rows: row = rows.get()
		except peewee.OperationalError, err:
			# ALARM
			ev.set()
			problem_db = True
			logger.debug("CANNOT READ THE DB [%s]", err)
			
		if row:
			logger.info("When: %s; Where: %s, Temp: %s" % (row.twhen, row.tplace, row.tvalue))
			problem_heat = True if row.tvalue > MAX_TEMP else False
			if problem_heat:
				logger.warning("ALARM: TOO HOT IN %s! TEMP=%s EXCEEDS MAX VALUE [%s]" % (row.tplace, row.tvalue, MAX_TEMP))

		alarm_flag(ev, problem_db or problem_heat) # Alarm flag gets raised or lowered depending on our problem status

		time.sleep(freq)
	logger.debug('Exiting')

def run_alarm(stop_threads, evW, evR):
	""" Yet another thread:
	It only cares about blinking some lights and buzzing the alarm whenever the alarm flag is raised.
	Not to be too nasty we are buzzing for a short period once in a while
	- that's the default mode, but you can be as nasty as you like using -n and -r command line options.
	"""
	evFlash = threading.Event() # flashing if this is set
	evBuzz = threading.Event() # ringing if this is set
	tFlash = None
	tBuzz = None

	last_buzzing = {'when': None}
	def ysn_time_to_buzz():
		new_time = time.time()
		if (last_buzzing['when'] is None) or (new_time - last_buzzing['when']) > DELAY_BETWEEN_NOISES:
			last_buzzing['when'] = new_time
			return True
		return False

	logger.debug("Entering")
	while not stop_threads.isSet():
		if evW.isSet() or evR.isSet():
			logger.debug('OMG OMG ALARM FIRE WATER BURN!!')
			if not evFlash.isSet():
				evFlash.set()
				if (not tFlash) or (not tFlash.isAlive()):
					tFlash = threading.Thread(name='Flasher', target=io_flash_lights, args=(evFlash,))
					tFlash.start()
			if ysn_time_to_buzz():
				if not evBuzz.isSet():
					evBuzz.set()
					if (not tBuzz) or (not tBuzz.isAlive()):
						tBuzz = threading.Thread(name='Buzzer', target=io_activate_buzzer, args=(evBuzz,NOISE_DURATION))
						tBuzz.start()
		else:
			evFlash.clear()
			evBuzz.clear()
		time.sleep(FREQ_ALARM)

	evFlash.clear()
	evBuzz.clear()
	logger.debug("Exiting")

def set_logger(log_level_screen, log_level_syslog):
	#logging.basicConfig(level = logging.DEBUG,
	#					format = '%(asctime)s [%(levelname)s] (%(threadName)-10s) %(message)s',)

	global logger
	logger = logging.getLogger('peewee') # we use this name to see the SQLs (if DEBUG level is ON)
	logger.setLevel(logging.DEBUG)
	
	syslog_hand = logging.handlers.SysLogHandler(address = '/dev/log')
	syslog_hand.setLevel(log_level_syslog)
	
	screen_hand = logging.StreamHandler()
	screen_hand.setLevel(log_level_screen)
	
	formatter = logging.Formatter('%(asctime)s [%(levelname)s] (%(threadName)-10s) %(message)s')
	syslog_hand.setFormatter(formatter)
	screen_hand.setFormatter(formatter)
	
	logger.addHandler(syslog_hand)
	logger.addHandler(screen_hand)

def read_settings():
	global MODE, DB_HOST, DB_NAME, DB_USER, DB_PWD, SENSOR_PLACE, FREQ_SENSOR, FREQ_CONTROL, MAX_TEMP, NOISE_DURATION, DELAY_BETWEEN_NOISES
	try:
		opts, args = getopt.getopt(sys.argv[1:], "sch:m:d:u:p:a:b:t:n:r:L:")
	except getopt.GetoptError:
		usage()
		sys.exit(2)

	log_level_screen = logging.INFO
	log_level_syslog = logging.WARNING

	for o, a in opts:
		if o == "-h":
			DB_HOST = a
		elif o == "-m":
			MODE = a
		elif o == "-d":
			DB_NAME = a
		elif o == "-c":
			log_level_screen = logging.DEBUG
		elif o == "-s":
			log_level_syslog = logging.DEBUG
		elif o == '-u':
			DB_USER = a
		elif o == '-p':
			DB_PWD = a
		elif o == '-L':
			SENSOR_PLACE = a
		elif o == '-a':
			FREQ_SENSOR = int(a)
			if FREQ_SENSOR < 10: FREQ_SENSOR = 10
		elif o == '-b':
			FREQ_CONTROL = int(a)
			if FREQ_CONTROL < 30: FREQ_CONTROL = 30
		elif o == '-t':
			MAX_TEMP = float(a)
		elif o == '-n':
			NOISE_DURATION = int(a)
			if NOISE_DURATION < 1: NOISE_DURATION = 1
			if NOISE_DURATION > 60: NOISE_DURATION = 60
		elif o == '-r':
			DELAY_BETWEEN_NOISES = int(a)
			if DELAY_BETWEEN_NOISES < 1: DELAY_BETWEEN_NOISES = 1

	if (not DB_HOST) or (MODE.lower() not in ['sensor','controller','both']):
		usage()
		sys.exit(2)
	set_logger(log_level_screen, log_level_syslog)

def main():
	global db
	read_settings()
	init_gpio()
	
	# what shall we do?
	mode = MODE.lower()
	if mode not in ['both', 'sensor', 'controller']: mode = 'both'
	ysn_sensor = mode in ['both','sensor']
	ysn_control = mode in ['both','controller']

	# init peewee - connect to the DB and create the table if needed
	db = peewee.MySQLDatabase(DB_NAME, user=DB_USER, passwd=DB_PWD, host=DB_HOST)
	db.connect()
	Tvalue._meta.database = db
	db.create_tables([Tvalue])

	# define and start workers
	stop_threads = threading.Event() # signals when to terminate all threads
	evW = threading.Event() # signals a WRITE error
	evR = threading.Event() # signals a READ error
	if ysn_sensor:
		temp = threading.Thread(name="TempSensor", target=run_gather_temp, args=(FREQ_SENSOR, stop_threads, evW))
		temp.start()
	if ysn_control:
		control = threading.Thread(name="Controller", target=run_checkdb, args=(FREQ_CONTROL, stop_threads, evR))
		control.start()
		alarm = threading.Thread(name="AlarmBell", target=run_alarm, args=(stop_threads, evW, evR))
		#alarm.setDaemon(True)
		alarm.start()

	# ...and now sit back, relax, and wait for the Ctrl-C
	try:
		while True:
			#logger.debug('Waiting for Ctrl-C...')
			time.sleep(1)
	except KeyboardInterrupt:
		pass
	finally:
		destroy(stop_threads, evW, evR)
	
if __name__ == "__main__":
	main()
