# Adjustable regulator: 12 V to 5 V at 100 mA, from the LM317's datasheet

12 V within 5% comes in on a two-pin header. Make 5 V with an LM317 in
SOT-223, set by a 240 ohm resistor from output to the adjust pin and a
715 ohm resistor from the adjust pin to ground (both 1%). Put 100 nF on
the input and 1 uF on the output. The 5 V rail feeds 100 mA.

The regulator's values are from Texas Instruments' datasheet (SLVS044Z,
April 2025): reference voltage 1.2 V to 1.3 V, which is 1.25 V within 4%,
for 3 V or more across the regulator (page 8); adjust-pin current at most
100 uA (page 8); current limit at least 1.5 A (page 8); 66.8 degC/W from
junction to air in SOT-223 for the legacy chip (page 7); and the output
equation Vout = Vref (1 + R2 / R1) + Iadj R2 (page 16).

The rail must be 5 V within 5%, and the junction under 125 degC at 40
degC ambient.

The output works out to 4.704 V to 5.323 V over the reference's and the
resistors' tolerances, which is outside 4.75 V to 5.25 V at both ends:
the reference alone moves the output by 4%. So the rail requirement
fails, and Kairo must refuse the design and say why, not write it.
