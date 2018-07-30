# raspberry_temperature_alarm
A simple temperature monitor for Raspberry Pi using Python and MySql

The aim of the project is to create an annoying buzzer which gets activated whenever the measured temperature gets too high
(or some other error happens).

The python program will be running either:
- as a Sensor (measures the temperature and sends to a remote MySql server)
- or as a Controller (reads the DB and raises an alarm if the read temperature is too high)
- or it could act as both.

On one hand it makes no sense for the Controller to read the remote DB when it can just ask the Sensor to have the current value.
In fact, there's a deeper meaning here: we'll use this device during a cyber training,
so if the DB gets hacked the Blue team will have a harder time due to the noises this Thing of the Internet makes.

Read the Wiki (https://github.com/ausrius/raspberry_temperature_alarm/wiki) on how to set up everything and run it.
