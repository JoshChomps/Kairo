# Reverse-polarity protection with a series Schottky diode

12 V comes in on a two-pin header and may be connected backwards. Put a
Schottky diode (forward voltage 0.4 V) in series with the positive lead,
so nothing flows when the input is reversed, then 10 uF rated 25 V to
ground, and bring the protected rail, about 11.6 V, out on a second
two-pin header. The protected rail feeds 500 mA.
