# Temperature sensor on an I2C bus

A four-pin header brings in 3.3 V, ground, SDA and SCL. Put an LM75B
temperature sensor on the bus at address 0x48 (A0, A1 and A2 to ground),
with a 4.7k pull-up from each bus line to 3.3 V and 100 nF across the
sensor's supply. The sensor's over-temperature output is not used.
