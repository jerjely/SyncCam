/*
  SyncCam_Arduino - analog front end for the SyncCam app.
  Boards: Arduino Uno / Nano (ATmega328P), Nano Every, Uno R4 Minima / WiFi, Leonardo, Mega.
  Upload once with the Arduino IDE.

  Wiring (defaults; the pins can be changed from the SyncCam app, no re-upload needed)
    A0  <- TTL sync signal (the same TTL that goes to the Neuropixels / OneBox), 0-5 V
    A1  <- microphone module analog output (e.g. MAX4466 / MAX9814, biased at ~2.5 V)
    D8  -> IR LED (through a resistor or a transistor/MOSFET driver)
    GND -- common ground with the TTL source and the microphone

  Serial: 1 000 000 baud. While streaming, every sample is one 6-byte packet:
    [0xA5] [seq] [b0] [b1] [b2] [chk]
      bits  0- 9 of b0..b2 (little endian): A0 value (0..1023)
      bits 10-19                          : A1 value (0..1023)
      bit  20                             : IR LED state
      bit  21                             : TTL above threshold
      bits 22-23                          : 0
      seq = sample counter modulo 256, chk = 0x5A ^ seq ^ b0 ^ b1 ^ b2

  Commands from the computer (ASCII, one per line, ending with '\n'):
    ?            identify (only answered while NOT streaming):
                 "SYNCCAM 2 fs=5000 ttl=A0 mic=A1 ir=D8"
    S            start streaming (sample counter restarts at 0)
    X            stop streaming
    F<hz>        sample rate, 100..10000 Hz   (e.g. F5000)
    I<m>         IR LED mode: 0 off, 1 on, 2 follow the TTL, 3 periodic pulses
    P<per>,<w>   pulse period and width in ms for mode 3 (e.g. P1000,50)
    T<thr>       TTL threshold in ADC units 0..1023 (default 512 = 2.5 V)
    A<t>,<m>     analog inputs for TTL and microphone, 0..7 = A0..A7 (e.g. A5,1)
                 (A6/A7 only on boards that have them, e.g. Nano, Nano Every)
    L<pin>       digital pin for the IR LED, 2..13 (e.g. L8)
*/

// analog inputs this board has, so channel n means pin An on every board
const uint8_t ANALOG_PINS[] = {A0, A1, A2, A3, A4, A5
#if defined(PIN_A6)
                               , A6
#endif
#if defined(PIN_A7)
                               , A7
#endif
};
const uint8_t N_ANALOG = sizeof(ANALOG_PINS);

uint8_t ttlCh = 0;              // index into ANALOG_PINS: 0 = A0, 5 = A5, ...
uint8_t micCh = 1;
uint8_t irPin = 8;               // digital pin
const unsigned long BAUD = 1000000UL;
const uint8_t PROTOCOL_VERSION = 2;

bool streaming = false;
unsigned long fsHz = 5000;
unsigned long periodUs = 200;
unsigned long nextSampleUs = 0;
uint8_t seq = 0;

uint8_t irMode = 0;              // 0 off, 1 on, 2 follow TTL, 3 pulses
bool irState = false;
unsigned long pulsePeriodUs = 1000000UL;
unsigned long pulseWidthUs = 50000UL;
unsigned long pulseStartUs = 0;  // start of the current pulse period
int ttlThreshold = 512;
const int TTL_HYST = 40;         // hysteresis around the threshold
bool ttlHigh = false;

char line[24];
uint8_t lineLen = 0;

// 10-bit readings on every board (the Uno R4 can do more, but the packet holds 10 bits)
void analogReadResolution10() {
#if defined(ARDUINO_ARCH_RENESAS) || defined(ARDUINO_ARCH_SAMD) || defined(ARDUINO_ARCH_MBED)
  analogReadResolution(10);
#endif
}

// Read an input twice and keep the second value. The ADC's sample capacitor keeps some
// charge from the previously read input; the throw-away read removes that cross-talk
// (otherwise the TTL leaks into the microphone channel, worst when an input is unconnected).
int readAnalog(uint8_t ch) {
  analogRead(ANALOG_PINS[ch]);
  return analogRead(ANALOG_PINS[ch]);
}

void setIr(bool on) {
  if (on != irState) {
    irState = on;
    digitalWrite(irPin, on ? HIGH : LOW);
  }
}

void updateIr(unsigned long now) {
  switch (irMode) {
    case 0: setIr(false); break;
    case 1: setIr(true); break;
    case 2: setIr(ttlHigh); break;
    case 3: {
      while ((long)(now - (pulseStartUs + pulsePeriodUs)) >= 0) pulseStartUs += pulsePeriodUs;
      setIr((now - pulseStartUs) < pulseWidthUs);
      break;
    }
  }
}

void handleLine() {
  line[lineLen] = 0;
  char c = line[0];
  long v = atol(line + 1);
  switch (c) {
    case '?':
      if (!streaming) {
        Serial.print(F("SYNCCAM "));
        Serial.print(PROTOCOL_VERSION);
        Serial.print(F(" fs="));
        Serial.print(fsHz);
        Serial.print(F(" ttl=A"));
        Serial.print(ttlCh);
        Serial.print(F(" mic=A"));
        Serial.print(micCh);
        Serial.print(F(" ir=D"));
        Serial.println(irPin);
      }
      break;
    case 'S':
      seq = 0;
      nextSampleUs = micros();
      streaming = true;
      break;
    case 'X':
      streaming = false;
      break;
    case 'F':
      if (v >= 100 && v <= 10000) {
        fsHz = v;
        periodUs = 1000000UL / fsHz;
      }
      break;
    case 'I':
      if (v >= 0 && v <= 3) {
        irMode = (uint8_t)v;
        pulseStartUs = micros();
      }
      break;
    case 'P': {
      char *comma = strchr(line, ',');
      long w = comma ? atol(comma + 1) : 0;
      if (v >= 2 && w >= 1 && w < v) {
        pulsePeriodUs = (unsigned long)v * 1000UL;
        pulseWidthUs = (unsigned long)w * 1000UL;
        pulseStartUs = micros();
      }
      break;
    }
    case 'T':
      if (v >= 1 && v <= 1022) ttlThreshold = (int)v;
      break;
    case 'A': {
      char *comma = strchr(line, ',');
      long m = comma ? atol(comma + 1) : -1;
      if (!streaming && v >= 0 && v < N_ANALOG && m >= 0 && m < N_ANALOG) {
        ttlCh = (uint8_t)v;
        micCh = (uint8_t)m;
      }
      break;
    }
    case 'L':
      if (!streaming && v >= 2 && v <= 13 && v != irPin) {
        digitalWrite(irPin, LOW);
        pinMode(irPin, INPUT);
        irPin = (uint8_t)v;
        pinMode(irPin, OUTPUT);
        digitalWrite(irPin, irState ? HIGH : LOW);
      }
      break;
  }
}

void readCommands() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (lineLen) handleLine();
      lineLen = 0;
    } else if (lineLen < sizeof(line) - 1) {
      line[lineLen++] = c;
    }
  }
}

void setup() {
  pinMode(irPin, OUTPUT);
  digitalWrite(irPin, LOW);
  // Faster ADC clock where the board allows it: one analogRead takes ~13 us instead of
  // ~110 us, which allows both channels at up to 10 kHz with 10-bit values.
#if defined(ADCSRA)                                   // Uno, Nano, Leonardo, Mega
  ADCSRA = (ADCSRA & ~0x07) | 0x04;
#elif defined(ADC0) && defined(ADC_PRESC_gm)          // Nano Every (ATmega4809)
  ADC0.CTRLC = (ADC0.CTRLC & ~ADC_PRESC_gm) | ADC_PRESC_DIV16_gc;
#endif
  analogReadResolution10();
  Serial.begin(BAUD);
}

void loop() {
  readCommands();
  unsigned long now = micros();
  if (streaming && (long)(now - nextSampleUs) >= 0) {
    nextSampleUs += periodUs;
    int a0 = readAnalog(ttlCh);
    int a1 = readAnalog(micCh);
    if (ttlHigh) {
      if (a0 < ttlThreshold - TTL_HYST) ttlHigh = false;
    } else if (a0 > ttlThreshold + TTL_HYST) {
      ttlHigh = true;
    }
    updateIr(now);
    uint32_t bits = (uint32_t)(a0 & 0x3FF) | ((uint32_t)(a1 & 0x3FF) << 10) |
                    ((uint32_t)irState << 20) | ((uint32_t)ttlHigh << 21);
    uint8_t pkt[6];
    pkt[0] = 0xA5;
    pkt[1] = seq;
    pkt[2] = bits & 0xFF;
    pkt[3] = (bits >> 8) & 0xFF;
    pkt[4] = (bits >> 16) & 0xFF;
    pkt[5] = 0x5A ^ pkt[1] ^ pkt[2] ^ pkt[3] ^ pkt[4];
    Serial.write(pkt, 6);
    seq++;
  } else if (!streaming) {
    // keep the IR LED working (and the TTL state fresh) when not streaming
    int a0 = analogRead(ANALOG_PINS[ttlCh]);
    if (ttlHigh) {
      if (a0 < ttlThreshold - TTL_HYST) ttlHigh = false;
    } else if (a0 > ttlThreshold + TTL_HYST) {
      ttlHigh = true;
    }
    updateIr(now);
  }
}
