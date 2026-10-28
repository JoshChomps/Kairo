# 5 V to 3.3 V regulator with a power LED

Take 5 V in on a two-pin header and make 3.3 V with an AMS1117-3.3 in
SOT-223. The 3.3 V rail feeds a microcontroller budgeted at 300 mA. Put
10 uF on the input and 22 uF on the output, both X5R 0805 with 10%
tolerance, and a power LED on the 3.3 V rail through a 1 k 1% resistor
(LED forward voltage 2.0 V).

Requirements: at least 22 uF effective between 3.3 V and ground; the
3.3 V rail within 3%; the regulator's junction at or below 125 degC in a
40 degC ambient, taking 90 degC/W; the LED resistor at no more than half
its 0.125 W rating.

The user gave the regulator's 1 A rating, 1.3 V dropout and 90 degC/W.
They did not give its quiescent current, its output tolerance, or part
numbers for the capacitors.
