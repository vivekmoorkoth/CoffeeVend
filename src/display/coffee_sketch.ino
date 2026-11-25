#include <SPI.h>
#include <Adafruit_GFX.h>
#include <Adafruit_GC9A01A.h>

#define TFT_DC  9
#define TFT_CS  10
#define TFT_RST 8

Adafruit_GC9A01A tft(TFT_CS, TFT_DC, TFT_RST);

const int SRC   = 80;   // incoming frames are 80x80
const int SCALE = 3;    // 80 x 3 = 240
const int CHUNK = 4;    // rows sent per request

uint8_t  buf[SRC * CHUNK];
uint16_t line[SRC * SCALE];

void expandRow(const uint8_t *row) {
  int j = 0;
  for (int i = 0; i < SRC; i++) {
    uint8_t c  = row[i];
    uint8_t r3 = (c >> 5) & 0x07;
    uint8_t g3 = (c >> 2) & 0x07;
    uint8_t b2 = c & 0x03;
    uint16_t r5 = (r3 << 2) | (r3 >> 1);
    uint16_t g6 = (g3 << 3) | g3;
    uint16_t b5 = (b2 << 3) | (b2 << 1) | (b2 >> 1);
    uint16_t col = (r5 << 11) | (g6 << 5) | b5;
    line[j++] = col;
    line[j++] = col;
    line[j++] = col;
  }
}

void setup() {
  Serial.begin(1000000);
  Serial.setTimeout(1000);
  tft.begin();
  tft.fillScreen(GC9A01A_BLACK);
}

void loop() {
  Serial.write('F');
  tft.startWrite();
  tft.setAddrWindow(0, 0, 240, 240);

  for (int y = 0; y < SRC; y += CHUNK) {
    Serial.write('R');
    if (Serial.readBytes(buf, sizeof(buf)) != sizeof(buf)) {
      tft.endWrite();
      return;
    }
    for (int r = 0; r < CHUNK; r++) {
      expandRow(buf + r * SRC);
      for (int s = 0; s < SCALE; s++) {
        tft.writePixels(line, SRC * SCALE);
      }
    }
  }

  tft.endWrite();
}