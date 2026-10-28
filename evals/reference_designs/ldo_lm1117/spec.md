# 5 V to 3.3 V with an LM1117-3.3, from its datasheet

The same circuit as `ldo_3v3`, with the regulator named and every one of
its values taken from Texas Instruments' datasheet for the LM1117
(SNOS412Q, January 2023), using the limits over 0 to 125 degC:

- output 3.235 V to 3.365 V for 0 to 800 mA and 4.75 V to 10 V in, which
  is 3.3 V within 1.97% (page 5);
- dropout at most 1.25 V at 500 mA (page 6);
- current limit at least 800 mA (page 6);
- quiescent current at most 10 mA (page 6);
- 61.6 degC/W from junction to air in SOT-223 (page 4).

- the output capacitor must be at least 10 uF with an equivalent series
  resistance from 0.3 ohm to 22 ohm (page 15).

5 V within 5% comes in on a two-pin header, with a 10 uF ceramic on the
input. The output capacitor is a 22 uF 10% tantalum whose ESR is 0.5 ohm
to 2 ohm (1 ohm typical); the 3.3 V rail feeds 300 mA and a power LED
(2.0 V) through 1k. The rail must be 3.3 V within 3%, the junction under
125 degC at 40 degC ambient, R1 at no more than half its rating, and the
output must have at least 10 uF of effective capacitance.

The rail, the capacitance, the regulator's load and its stability can
all be shown from these numbers and must pass. The junction temperature
cannot: the datasheet gives the thermal resistance as one figure for a
test board, with no range, and the LED's forward voltage has no
tolerance, which also leaves R1's power unverified. Kairo must say so
and must not pass either.
