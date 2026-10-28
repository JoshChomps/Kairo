# Refusal: a step-down converter whose divider cannot hold 5%

The same 12 V to 5 V converter as `buck_tps54202`, part for part: a
TPS54202 with its datasheet's values (SLVSD26C), the datasheet's own
divider of 100 kilohms over 13.3 kilohms at 1% each, a 15 uH inductor
and one 47 uF polymer output capacitor, feeding 1.5 A.

Here the rail must be 5 V within 5%, where the other design asks for 7%.

This cannot be met with these parts. The feedback voltage is 0.581 V to
0.611 V and each resistor is within 1%, so the rail is

- at most 0.611 V * (1 + 101 k / 13.167 k) = 5.298 V, and
- at least 0.581 V * (1 + 99 k / 13.433 k) = 4.863 V,

while 5 V within 5% is 4.75 V to 5.25 V. The top of the range is 48 mV
over. The divider is centred on 5.08 V, not 5 V, and the reference's
own 2.5% leaves too little room. The right answer is to say so with
the arithmetic and write nothing: not to loosen the requirement, and
not to pick other resistors the request did not ask for without saying
that the request as given fails.
