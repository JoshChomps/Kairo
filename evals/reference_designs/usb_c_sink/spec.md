# USB-C power sink at 5 V

A USB-C receptacle (USB 2.0, 16 pins) takes 5 V from a source. Put a
5.1k 1% resistor from each CC pin to ground so the source turns its
supply on, 10 uF across the bus, and bring 5 V and ground out on a
two-pin header. The data and sideband pins are not used; the shield goes
to ground.
