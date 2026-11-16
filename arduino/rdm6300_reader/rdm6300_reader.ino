#include <SoftwareSerial.h>

// RDM6300 TX -> Nano D2
// RDM6300 GND -> Nano GND
// RDM6300 VCC -> 5V (проверь маркировку именно своего модуля)
// D3 не используется, но SoftwareSerial требует указать TX-пин.
SoftwareSerial rfid(2, 3);

const byte FRAME_LENGTH = 14;
byte frameData[FRAME_LENGTH];
byte framePos = 0;

String lastUid = "";
unsigned long lastCardSeenAt = 0;
const unsigned long CARD_RELEASE_MS = 1500;

int hexValue(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  return -1;
}

bool parseHexByte(byte highChar, byte lowChar, byte &result) {
  int hi = hexValue((char)highChar);
  int lo = hexValue((char)lowChar);
  if (hi < 0 || lo < 0) return false;
  result = (byte)((hi << 4) | lo);
  return true;
}

void processFrame() {
  if (frameData[0] != 0x02 || frameData[13] != 0x03) return;

  // 10 ASCII hex chars = 5 data bytes; 2 chars = XOR checksum.
  byte dataBytes[5];
  byte receivedChecksum = 0;

  for (byte i = 0; i < 5; i++) {
    if (!parseHexByte(frameData[1 + i * 2],
                      frameData[2 + i * 2],
                      dataBytes[i])) return;
  }

  if (!parseHexByte(frameData[11], frameData[12], receivedChecksum)) return;

  byte calculatedChecksum = 0;
  for (byte i = 0; i < 5; i++) calculatedChecksum ^= dataBytes[i];

  if (calculatedChecksum != receivedChecksum) {
    Serial.println("RFID_ERROR:CHECKSUM");
    return;
  }

  // Сохраняем все 10 символов данных: так UID не теряет ведущие нули.
  String uid = "";
  for (byte i = 1; i <= 10; i++) uid += (char)frameData[i];

  unsigned long now = millis();

  // Один CARD event на одно прикладывание карты.
  // Повторное событие будет разрешено после паузы в чтении.
  if (uid != lastUid || now - lastCardSeenAt > CARD_RELEASE_MS) {
    Serial.print("CARD:");
    Serial.println(uid);
  }

  lastUid = uid;
  lastCardSeenAt = now;
}

void setup() {
  Serial.begin(9600); // USB к компьютеру
  rfid.begin(9600);   // UART RDM6300
  Serial.println("MONOPOLY_BANK_READY");
}

void loop() {
  while (rfid.available()) {
    byte incoming = (byte)rfid.read();

    if (incoming == 0x02) {
      framePos = 0;
      frameData[framePos++] = incoming;
      continue;
    }

    if (framePos == 0) continue;

    if (framePos < FRAME_LENGTH) {
      frameData[framePos++] = incoming;
    }

    if (framePos == FRAME_LENGTH) {
      processFrame();
      framePos = 0;
    }
  }
}
