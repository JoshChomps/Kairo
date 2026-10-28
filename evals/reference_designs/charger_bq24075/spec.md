# A one-cell charger with a BQ24075, its currents set by resistors

A single-cell lithium-ion charger with power-path control, built round
Texas Instruments' BQ24075 in its 3 mm by 3 mm RGT package. Every figure
for the chip comes from its datasheet (SLUS810N, revised October 2021):

- the fast-charge current is `ICHG = KISET / RISET` (equation 2, page
  25), with KISET 797 to 975 A*ohm (page 13), and the ISET resistor is
  590 ohms to 8.9 kilohms (page 8);
- the input current limit is `IIN-MAX = KILIM / RILIM` (equation 1, page
  21), with KILIM 1500 to 1720 A*ohm for limits of 500 mA to 1.5 A (page
  12); it applies with EN2 high and EN1 low (Table 7-2, page 9);
- IN is bypassed with 1 uF to 10 uF, and OUT and BAT each with 4.7 uF to
  47 uF of ceramic capacitance (page 8);
- CE low enables charging, SYSOFF low keeps the battery connected to the
  system, and neither may be left open; EN1 and EN2 may not be left open
  either (page 8);
- TMR left open gives the default safety timers, and TS takes a fixed
  10 kilohm resistor to ground when no thermistor is used (page 9);
- the RGT package is 44.5 degC/W from junction to air (page 11).

5 V within 5% comes in on a two-pin header with 1 uF on IN. EN2 is tied
to the input and EN1, CE and SYSOFF to ground, so the ILIM resistor sets
the input limit. The ISET resistor is 1.78 kilohms and the ILIM resistor
1.62 kilohms, each 1%; TS has its 10 kilohm resistor, and TMR, PGOOD and
CHG are left unconnected. The battery and the system each have a header
and 4.7 uF. The system draws 300 mA.

The charge current must be 400 mA to 600 mA and the input limit 800 mA
to 1.2 A, over the datasheet's spread of each factor and the resistors'
tolerances.

Both can be shown from these numbers and must pass: the charge current
is 443 mA to 553 mA and the input limit 917 mA to 1072 mA. Two things are
worth knowing:

- The datasheet's factors are spreads, not typical values. With KISET's
  typical 890 A*ohm alone the charge current would look like 500 mA to
  within the resistor's 1%; the spread of the factor is what sets the
  range.
- These are the currents the chip is programmed to. Whether it delivers
  them also depends on its thermal regulation, the battery's state and
  the input limit, which nothing here checks, and the charger is not
  simulated.
