#include "stream.h"

#include <Arduino.h>

#if __has_include("flybot_config.h")
#include "flybot_config.h"
#else
#include "flybot_config.example.h"
#endif

#ifdef STREAM_PORT
#include <base64.h>
#include <esp_http_server.h>
#include <img_converters.h>

#include "params.h"
#include "voice.h"

static constexpr int W = 160, H = 120;
static constexpr uint32_t FRAME_MS = 200;  // 5 fps is enough to watch and keeps the CPU for the brain
static uint8_t view[W * H];
static SemaphoreHandle_t lock;
static volatile uint32_t viewSeq = 0;
static volatile int clients = 0;
static volatile bool rawView = false;  // plain camera image instead of the detector's view
static String status = "{}";
static String authHeader;  // "Basic ..." for MQTT_USER / MQTT_PASSWORD; empty = open

void streamFrame(const uint8_t* luma, const uint8_t* changed, int w, int h, int minX, int minY, int maxX,
                 int maxY, bool skipped) {
  static uint32_t last = 0;
  if (!clients || w != W || h != H || millis() - last < FRAME_MS) return;
  last = millis();
  if (xSemaphoreTake(lock, 0) != pdTRUE) return;
  if (rawView) {
    memcpy(view, luma, sizeof view);
  } else {
    for (int i = 0; i < W * H; ++i) view[i] = changed[i] ? 255 : luma[i] / 2;  // motion bright, scene dim
  }
  if (maxX >= 0) {
    for (int x = minX; x <= maxX; ++x) view[minY * W + x] = view[maxY * W + x] = 200;
    for (int y = minY; y <= maxY; ++y) view[y * W + minX] = view[y * W + maxX] = 200;
  }
  if (skipped) {  // a white frame: the head was turning, the detector ignored this one
    for (int x = 0; x < W; ++x) view[x] = view[(H - 1) * W + x] = 255;
    for (int y = 0; y < H; ++y) view[y * W] = view[y * W + W - 1] = 255;
  }
  ++viewSeq;
  xSemaphoreGive(lock);
}

void streamSetStatus(const String& json) {
  if (xSemaphoreTake(lock, pdMS_TO_TICKS(5)) != pdTRUE) return;
  status = json;
  xSemaphoreGive(lock);
}

static bool authorized(httpd_req_t* req) {
  if (authHeader.isEmpty()) return true;
  char got[128] = "";
  if (httpd_req_get_hdr_value_str(req, "Authorization", got, sizeof got) == ESP_OK && authHeader == got) return true;
  httpd_resp_set_status(req, "401 Unauthorized");
  httpd_resp_set_hdr(req, "WWW-Authenticate", "Basic realm=\"StackChan\"");
  httpd_resp_send(req, nullptr, 0);
  return false;
}

static esp_err_t streamHandler(httpd_req_t* req) {
  // its own port is another origin, so the page passes the credentials as a token
  char query[160] = "", token[128] = "";
  httpd_req_get_url_query_str(req, query, sizeof query);
  httpd_query_key_value(query, "token", token, sizeof token);
  if (authHeader.length() && authHeader.substring(6) != token && !authorized(req)) return ESP_OK;
  rawView = strstr(query, "raw=1") != nullptr;
  static const char* BOUNDARY = "\r\n--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n";
  httpd_resp_set_type(req, "multipart/x-mixed-replace;boundary=frame");
  ++clients;
  uint32_t seen = 0;
  esp_err_t res = ESP_OK;
  while (res == ESP_OK) {
    if (viewSeq == seen) {
      delay(20);
      continue;
    }
    uint8_t* jpg = nullptr;
    size_t len = 0;
    xSemaphoreTake(lock, portMAX_DELAY);
    seen = viewSeq;
    bool ok = fmt2jpg(view, sizeof view, W, H, PIXFORMAT_GRAYSCALE, 70, &jpg, &len);
    xSemaphoreGive(lock);
    if (!ok) break;
    char head[96];
    int n = snprintf(head, sizeof head, BOUNDARY, unsigned(len));
    res = httpd_resp_send_chunk(req, head, n);
    if (res == ESP_OK) res = httpd_resp_send_chunk(req, (const char*)jpg, len);
    free(jpg);
  }
  --clients;
  return res;
}

static esp_err_t statusHandler(httpd_req_t* req) {
  if (!authorized(req)) return ESP_OK;
  xSemaphoreTake(lock, portMAX_DELAY);
  String copy = status;
  xSemaphoreGive(lock);
  // tell the page where the stream is (port + 1) and the token it needs there
  copy = "{\"stream\":\":" + String(STREAM_PORT + 1) + "/stream?token=" + (authHeader.length() ? authHeader.substring(6) : String()) +
         "\"," + copy.substring(1);
  httpd_resp_set_type(req, "application/json");
  httpd_resp_set_hdr(req, "Cache-Control", "no-store");
  return httpd_resp_send(req, copy.c_str(), copy.length());
}

// GET /set?name=diff_threshold&value=30  |  GET /set?reset=1
static esp_err_t setHandler(httpd_req_t* req) {
  if (!authorized(req)) return ESP_OK;
  char query[96] = "", name[32] = "", value[24] = "";
  httpd_req_get_url_query_str(req, query, sizeof query);
  bool ok;
  if (strstr(query, "reset=1")) {
    ok = paramsReset();
  } else {
    ok = httpd_query_key_value(query, "name", name, sizeof name) == ESP_OK &&
         httpd_query_key_value(query, "value", value, sizeof value) == ESP_OK && paramsSet(name, value);
  }
  httpd_resp_set_type(req, "application/json");
  if (!ok) httpd_resp_set_status(req, "400 Bad Request");
  String body = ok ? paramsJson() : String("{\"error\":\"unknown name or out of range\"}");
  return httpd_resp_send(req, body.c_str(), body.length());
}

// GET /rec.wav: the last sentence the mic captured, to check what the ASR got.
static esp_err_t recHandler(httpd_req_t* req) {
  if (!authorized(req)) return ESP_OK;
  const int16_t* pcm = nullptr;
  const uint32_t bytes = voiceRecording(&pcm) * 2;
  uint8_t h[44];
  const uint32_t riff = 36 + bytes, rate = 16000, byteRate = 32000, fmtLen = 16;
  const uint16_t fmt = 1, ch = 1, align = 2, bits = 16;
  memcpy(h, "RIFF", 4), memcpy(h + 4, &riff, 4), memcpy(h + 8, "WAVEfmt ", 8), memcpy(h + 16, &fmtLen, 4);
  memcpy(h + 20, &fmt, 2), memcpy(h + 22, &ch, 2), memcpy(h + 24, &rate, 4), memcpy(h + 28, &byteRate, 4);
  memcpy(h + 32, &align, 2), memcpy(h + 34, &bits, 2), memcpy(h + 36, "data", 4), memcpy(h + 40, &bytes, 4);
  httpd_resp_set_type(req, "audio/wav");
  httpd_resp_send_chunk(req, (const char*)h, sizeof h);
  for (uint32_t off = 0; pcm && off < bytes; off += 4096) {
    if (httpd_resp_send_chunk(req, (const char*)pcm + off, min(4096u, bytes - off)) != ESP_OK) return ESP_FAIL;
  }
  return httpd_resp_send_chunk(req, nullptr, 0);
}

static const char PAGE[] = R"HTML(<!doctype html><html lang=th><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>StackChan monitor</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#1d2330;--mute:#6b7383;--line:#e3e6eb;--acc:#2f6fed;--ok:#1a9b5a;--warn:#c9821b}
@media(prefers-color-scheme:dark){:root{--bg:#121417;--card:#1b1e23;--ink:#e6e8eb;--mute:#9098a6;--line:#2a2e35;--acc:#6b9bff;--ok:#3ccf87;--warn:#e7a948}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,sans-serif}
header{padding:12px 16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap;border-bottom:1px solid var(--line);background:var(--card)}
h1{font-size:16px;margin:0}.pill{padding:2px 8px;border-radius:99px;background:var(--line);font-size:12px}
.pill.ok{background:var(--ok);color:#fff}.pill.bad{background:var(--warn);color:#fff}
main{display:grid;gap:12px;padding:12px 16px;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));max-width:1200px;margin:auto}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}
h2{font-size:13px;margin:0 0 8px;color:var(--mute);font-weight:600;text-transform:uppercase;letter-spacing:.04em}
img{width:100%;image-rendering:pixelated;border-radius:6px;background:#000;aspect-ratio:4/3}
table{width:100%;border-collapse:collapse}td{padding:3px 0;border-bottom:1px solid var(--line)}td:last-child{text-align:right;font-variant-numeric:tabular-nums}
.bar{height:8px;background:var(--line);border-radius:4px;position:relative;overflow:hidden;margin:6px 0}
.bar i{position:absolute;left:0;top:0;bottom:0;background:var(--acc)}.bar b{position:absolute;top:-2px;bottom:-2px;width:2px;background:var(--warn)}
label{display:block;margin:8px 0 2px}label span{float:right;color:var(--mute);font-variant-numeric:tabular-nums}
input[type=range]{width:100%}button{font:inherit;padding:4px 10px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}
.row{display:flex;gap:8px;align-items:center;margin-top:8px}.mute{color:var(--mute);font-size:12px}
</style>
<header><h1>StackChan monitor</h1><span id=wifi class=pill>-</span><span id=mqtt class=pill>MQTT</span><span id=voice class=pill>voice</span><span class=mute id=up></span></header>
<main>
<section><h2>Camera</h2><img id=cam alt="camera">
<div class=row><button id=mode>Show plain image</button><span class=mute id=camtxt></span></div>
<p class=mute>Detector view: bright = changed pixels, box = motion, white border = skipped (head turning)</p></section>
<section><h2>Brain</h2><table id=brain></table></section>
<section><h2>Head &amp; sensors</h2><table id=sens></table></section>
<section><h2>Microphone</h2><div class=bar><i id=lvl></i><b id=thr></b></div><table id=mic></table></section>
<section><h2>Tuning</h2><div id=params></div><div class=row><button id=reset>Reset to defaults</button><span class=mute id=msg></span></div></section>
</main>
<script>
const $=id=>document.getElementById(id);let raw=false;
const LABEL={diff_threshold:'Pixel change threshold',min_motion_px:'Min changed pixels',max_motion_frac:'Max changed fraction (skip)',
ego_deg_s:'Skip while head turns faster than (°/s)',tone_volume_pct:'Brain tones volume (%)',voice_volume:'Speech volume (0-255)',
voice_gain:'Speech gain (x)',vad_min_rms:'Mic: min speech level',vad_ratio:'Mic: x room noise'};
let streamPath='';
function cam(){if(!streamPath)return;$('cam').src=location.protocol+'//'+location.hostname+streamPath+(raw?'&raw=1':'');$('mode').textContent=raw?'Show detector view':'Show plain image'}
$('mode').onclick=()=>{raw=!raw;cam()};
function rows(el,obj){el.innerHTML=Object.entries(obj).map(([k,v])=>`<tr><td>${k}<td>${v}`).join('')}
const f=(x,d=2)=>typeof x==='number'?x.toFixed(d):Array.isArray(x)?x.map(v=>f(v,d)).join(', '):x;
let built=false;
function params(p){
 if(!built){$('params').innerHTML=Object.entries(p).map(([k,v])=>{const st=v.float?((v.max-v.min)/100):1;
  return `<label>${LABEL[k]||k}<span id=v_${k}></span></label><input type=range id=r_${k} min=${v.min} max=${v.max} step=${st}>`}).join('');
  for(const k in p){const r=$('r_'+k);r.oninput=()=>$('v_'+k).textContent=r.value;
   r.onchange=async()=>{const res=await fetch(`/set?name=${k}&value=${r.value}`);$('msg').textContent=res.ok?'saved '+k+' = '+r.value:'rejected'}}
  built=true}
 for(const k in p){const r=$('r_'+k);if(document.activeElement!==r){r.value=p[k].value;$('v_'+k).textContent=f(p[k].value,p[k].float?2:0)}}}
$('reset').onclick=async()=>{await fetch('/set?reset=1');$('msg').textContent='defaults restored'};
async function tick(){try{const s=await(await fetch('/status')).json();
 if(!streamPath){streamPath=s.stream;cam()}
 $('wifi').textContent=`${s.wifi.ssid} ${s.wifi.rssi} dBm`;$('wifi').className='pill '+(s.wifi.rssi>-70?'ok':'bad');
 $('mqtt').className='pill '+(s.mqtt?'ok':'bad');$('up').textContent=`${s.wifi.ip} · up ${s.uptime}s · ${f(s.fps,1)} fps · ${f(s.cmd_hz,1)} cmd/s`;
 $('voice').textContent='voice: '+s.voice.state;$('voice').className='pill '+(s.voice.state==='off'||s.voice.state==='disabled'?'':'ok');
 $('camtxt').textContent=`${s.cam.n} px changed`+(s.cam.skipped?' · skipped':s.cam.detected?' · motion':'');
 const b=s.brain||{};rows($('brain'),Object.fromEntries(Object.entries(Object.assign({face:s.face},b)).map(([k,v])=>[k,f(v)])));
 rows($('sens'),{pan:f(s.servo.pan,1)+'°',tilt:f(s.servo.tilt,1)+'°',gyro:f(s.imu.gyro,1),accel:f(s.imu.accel),proximity:s.ps,light:s.als,'free heap':s.heap,'free PSRAM':s.psram});
 const v=s.voice,top=Math.max(v.threshold*3,1000);$('lvl').style.width=Math.min(100,100*v.level/top)+'%';$('thr').style.left=Math.min(100,100*v.threshold/top)+'%';
 rows($('mic'),{level:v.level,'room noise':f(v.noise,0),'speech threshold':f(v.threshold,0),bridge:v.url||'-'});
 params(s.params)}catch(e){$('up').textContent='offline'}setTimeout(tick,300)}
tick();
</script></html>)HTML";

static esp_err_t indexHandler(httpd_req_t* req) {
  if (!authorized(req)) return ESP_OK;
  httpd_resp_set_type(req, "text/html; charset=utf-8");
  return httpd_resp_send(req, PAGE, sizeof PAGE - 1);
}

void streamBegin() {
  lock = xSemaphoreCreateMutex();
  if (strlen(MQTT_PASSWORD)) authHeader = "Basic " + base64::encode(String(MQTT_USER) + ":" + MQTT_PASSWORD);
  httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
  cfg.server_port = STREAM_PORT;
  cfg.ctrl_port = STREAM_PORT + 32768;
  // LWIP has 16 sockets for everything (MQTT, voice, both servers' listen + ctrl sockets):
  // keep the page to a few connections and drop the oldest when they run out
  cfg.max_open_sockets = 3;
  cfg.lru_purge_enable = true;
  httpd_handle_t server = nullptr;
  if (httpd_start(&server, &cfg) != ESP_OK) {
    log_e("stream: httpd_start failed");
    return;
  }
  // the MJPEG stream blocks its socket's handler, so it gets a server of its own
  httpd_config_t scfg = HTTPD_DEFAULT_CONFIG();
  scfg.server_port = STREAM_PORT + 1;
  scfg.ctrl_port = STREAM_PORT + 32769;
  scfg.max_open_sockets = 1;
  scfg.lru_purge_enable = true;  // a new viewer takes over the stream
  httpd_handle_t streamServer = nullptr;
  httpd_uri_t index = {"/", HTTP_GET, indexHandler, nullptr};
  httpd_uri_t st = {"/status", HTTP_GET, statusHandler, nullptr};
  httpd_uri_t set = {"/set", HTTP_GET, setHandler, nullptr};
  httpd_uri_t recUri = {"/rec.wav", HTTP_GET, recHandler, nullptr};
  httpd_uri_t stream = {"/stream", HTTP_GET, streamHandler, nullptr};
  httpd_register_uri_handler(server, &index);
  httpd_register_uri_handler(server, &st);
  httpd_register_uri_handler(server, &set);
  httpd_register_uri_handler(server, &recUri);
  if (httpd_start(&streamServer, &scfg) == ESP_OK) httpd_register_uri_handler(streamServer, &stream);
}

#else
void streamBegin() {}
void streamFrame(const uint8_t*, const uint8_t*, int, int, int, int, int, int, bool) {}
void streamSetStatus(const String&) {}
#endif
