# ============================================================
#  Sistema de Irrigação Inteligente para Horta Urbana
#  Plataforma : ESP32 DevKitC V4  (MicroPython v1.22.0)
#  Protocolo  : MQTT (HiveMQ público)
#  Simulador  : Wokwi — board-esp32-devkit-c-v4
#
#  Sensores   : DHT22 (temperatura e umidade do ar)
#               Potenciômetro → sensor capacitivo de solo
#               Push-button   → pluviômetro de báscula
#  Atuadores  : LED verde (GPIO 2) → bomba d'água
#               LED vermelho (GPIO 5) → alarme / bloqueio
#
#  Lógica automática:
#   • Liga bomba se umidade do solo < 40 %
#   • Desliga bomba se umidade do solo ≥ 60 %
#   • Bloqueia irrigação se chuva nas últimas 24 h ≥ 3 mm
#   • Janela de irrigação permitida: 06h00 – 09h00
#     (manhã cedo, menor evaporação — boa prática agrícola)
#
#  Tópicos MQTT publicados:
#   horta/iot/sensor/temp_ar      → temperatura do ar (°C)
#   horta/iot/sensor/umid_ar      → umidade relativa do ar (%)
#   horta/iot/sensor/umid_solo    → umidade do solo (%)
#   horta/iot/sensor/chuva_24h    → precipitação nas últ. 24 h (mm)
#   horta/iot/sistema/estado      → JSON com modo/bomba/hora/bloqueio
#   horta/iot/sistema/alarme      → "OK" ou "BLOQUEADO"
#
#  Tópico MQTT subscrito:
#   horta/iot/sistema/cmd         → JSON de controle remoto
#     {"mode": "AUTO"|"ON"|"OFF"}
#     {"force_hour": <0-23>}       (sobrescreve hora p/ testes)
#     {"reset_chuva": true}        (limpa contagem de chuva)
# ============================================================

import time, json, machine, network, dht, ubinascii
from umqtt.simple import MQTTClient

# ──────────────────────────────────────────────
#  CONFIGURAÇÕES DE REDE
# ──────────────────────────────────────────────
WIFI_SSID = "Wokwi-GUEST"
WIFI_PASS = ""

# Broker MQTT público — sem autenticação requerida
MQTT_BROKER    = "broker.hivemq.com"
MQTT_PORT      = 1883
MQTT_CLIENT_ID = b"horta-esp32-" + ubinascii.hexlify(machine.unique_id())

# ──────────────────────────────────────────────
#  TÓPICOS MQTT
# ──────────────────────────────────────────────
TOPIC_TEMP    = b"horta/iot/sensor/temp_ar"
TOPIC_HUM     = b"horta/iot/sensor/umid_ar"
TOPIC_SOLO    = b"horta/iot/sensor/umid_solo"
TOPIC_CHUVA   = b"horta/iot/sensor/chuva_24h"
TOPIC_ESTADO  = b"horta/iot/sistema/estado"
TOPIC_CMD     = b"horta/iot/sistema/cmd"
TOPIC_ALARME  = b"horta/iot/sistema/alarme"

# ──────────────────────────────────────────────
#  LIMIARES DE CONTROLE
# ──────────────────────────────────────────────
SOLO_LIGAR    = 40.0    # % — liga bomba abaixo deste valor
SOLO_DESLIGAR = 60.0    # % — desliga bomba acima deste valor
CHUVA_BLOQ_MM = 3.0     # mm/24h — bloqueia irrigação se atingido

# Janela de irrigação permitida (hora UTC local)
HORA_INICIO = 6    # 06h00
HORA_FIM    = 9    # 09h00  (exclusive: irrigação até 08h59)

# ──────────────────────────────────────────────
#  TEMPORIZAÇÃO DOS CICLOS
# ──────────────────────────────────────────────
CICLO_MQTT_SEG  = 2     # publica telemetria MQTT a cada N s
CICLO_KEEPALIVE = 25    # ping MQTT keepalive
CICLO_DHT_SEG   = 4     # leitura do DHT22 (sensor lento)

# ──────────────────────────────────────────────
#  MAPEAMENTO DE PINOS — ESP32 DevKitC V4
# ──────────────────────────────────────────────
PINO_DHT      = 15    # DHT22 — dados
PINO_SOLO_ADC = 34    # ADC solo (apenas leitura)
PINO_CHUVA    = 4     # pluviômetro de báscula (botão → GND)
PINO_BOMBA    = 2     # LED verde — simula relé da bomba
PINO_ALARME   = 5     # LED vermelho — alarme visual

# Pluviômetro: cada basculada = 0.2794 mm (padrão Davis VP2)
MM_POR_BASCULA  = 0.2794
JANELA_CHUVA_S  = 24 * 3600   # 24 horas em segundos

# ──────────────────────────────────────────────
#  MODOS DE OPERAÇÃO
# ──────────────────────────────────────────────
MODO_AUTO = 0    # controle automático pelos sensores
MODO_ON   = 1    # bomba ligada manualmente
MODO_OFF  = 2    # bomba desligada manualmente

# ──────────────────────────────────────────────
#  ESTADO GLOBAL DO SISTEMA
# ──────────────────────────────────────────────
estado = {
    "modo":          MODO_AUTO,
    "bomba":         False,
    "temp_ar":       None,
    "umid_ar":       None,
    "umid_solo":     None,
    "basculadas":    [],     # timestamps das basculadas nas últimas 24 h
    "hora_forcada":  None,
    "bloqueado":     False,
}

_cache_mqtt = {"temp": None, "umid": None, "solo": None,
               "chuva": None, "estado": None}

# ──────────────────────────────────────────────
#  INICIALIZAÇÃO DO HARDWARE
# ──────────────────────────────────────────────
sensor_dht = dht.DHT22(machine.Pin(PINO_DHT))

adc_solo = machine.ADC(machine.Pin(PINO_SOLO_ADC))
try:
    adc_solo.atten(machine.ADC.ATTN_11DB)   # fundo de escala ~3,3 V
except Exception:
    pass

bomba  = machine.Pin(PINO_BOMBA,  machine.Pin.OUT); bomba.off()
alarme = machine.Pin(PINO_ALARME, machine.Pin.OUT); alarme.off()

pino_chuva    = machine.Pin(PINO_CHUVA, machine.Pin.IN, machine.Pin.PULL_UP)
_ultimo_chuva = 1    # último estado lido do pluviômetro

# ──────────────────────────────────────────────
#  FUNÇÕES DE SENSORES
# ──────────────────────────────────────────────
def verificar_pluviometro():
    """Detecta borda descendo no pluviômetro e registra o timestamp."""
    global _ultimo_chuva
    val = pino_chuva.value()
    ts  = time.time()
    if _ultimo_chuva == 1 and val == 0:
        estado["basculadas"].append(ts)
    _ultimo_chuva = val
    # descarta registros com mais de 24 h
    corte = ts - JANELA_CHUVA_S
    estado["basculadas"] = [t for t in estado["basculadas"] if t >= corte]


def chuva_mm_24h():
    """Retorna a precipitação acumulada nas últimas 24 horas em mm."""
    return round(len(estado["basculadas"]) * MM_POR_BASCULA, 3)


def ler_umidade_solo():
    """Lê o ADC e converte para umidade percentual (0–100 %)."""
    raw = adc_solo.read()
    raw = max(0, min(4095, raw))
    # Solo seco → ADC alto; solo úmido → ADC baixo
    pct = 100.0 - (raw / 4095.0) * 100.0
    return round(pct, 1)


def hora_atual():
    """Retorna a hora atual (0–23). Usa override se definido (para testes)."""
    if estado["hora_forcada"] is not None:
        return int(estado["hora_forcada"]) % 24
    try:
        return time.localtime()[3]
    except Exception:
        return 7    # fallback dentro da janela permitida


def dentro_da_janela(hora):
    """Verifica se a hora está dentro da janela de irrigação permitida."""
    return HORA_INICIO <= hora < HORA_FIM


# ──────────────────────────────────────────────
#  CONEXÃO WI-FI
# ──────────────────────────────────────────────
def conectar_wifi():
    sta = network.WLAN(network.STA_IF)
    if not sta.active():
        sta.active(True)
    if not sta.isconnected():
        print("[WiFi] Conectando a:", WIFI_SSID)
        sta.connect(WIFI_SSID, WIFI_PASS)
        for _ in range(120):
            if sta.isconnected():
                break
            time.sleep(0.25)
    status = "OK" if sta.isconnected() else "FALHOU"
    print("[WiFi]", status, sta.ifconfig())
    time.sleep(0.5)


# ──────────────────────────────────────────────
#  CALLBACK MQTT — RECEBE COMANDOS REMOTOS
# ──────────────────────────────────────────────
def ao_receber_cmd(topico, mensagem):
    print("[MQTT RX]", topico, mensagem)
    try:
        dados = json.loads(mensagem)
        if "mode" in dados:
            m = str(dados["mode"]).upper()
            if   m == "AUTO": estado["modo"] = MODO_AUTO
            elif m == "ON":   estado["modo"] = MODO_ON
            elif m == "OFF":  estado["modo"] = MODO_OFF
        if "force_hour" in dados:
            estado["hora_forcada"] = int(dados["force_hour"])
        if dados.get("reset_chuva"):
            estado["basculadas"].clear()
            print("[CMD] Contagem de chuva zerada.")
    except Exception as e:
        print("[CMD] Erro ao interpretar:", e)


# ──────────────────────────────────────────────
#  CONEXÃO MQTT
# ──────────────────────────────────────────────
def conectar_mqtt():
    c = MQTTClient(MQTT_CLIENT_ID, MQTT_BROKER, MQTT_PORT, keepalive=60)
    c.set_callback(ao_receber_cmd)
    c.connect()
    c.subscribe(TOPIC_CMD)
    print("[MQTT] Conectado a", MQTT_BROKER, "| Inscrito em", TOPIC_CMD)
    return c


# ──────────────────────────────────────────────
#  LÓGICA DE CONTROLE DA BOMBA
# ──────────────────────────────────────────────
def aplicar_logica():
    hora   = hora_atual()
    janela = dentro_da_janela(hora)
    chuva  = chuva_mm_24h()
    solo   = estado["umid_solo"] or 0.0

    # Determine bloqueio: chuva excessiva OU fora da janela
    bloqueado = (chuva >= CHUVA_BLOQ_MM) or (not janela)
    estado["bloqueado"] = bloqueado
    alarme.value(1 if bloqueado else 0)

    # Modo forçado manual tem prioridade sobre tudo
    if estado["modo"] == MODO_ON:
        bomba.on();  estado["bomba"] = True;  return
    if estado["modo"] == MODO_OFF:
        bomba.off(); estado["bomba"] = False; return

    # Automático — respeita bloqueios
    if bloqueado:
        bomba.off(); estado["bomba"] = False; return

    if solo < SOLO_LIGAR:
        bomba.on();  estado["bomba"] = True
    elif solo >= SOLO_DESLIGAR:
        bomba.off(); estado["bomba"] = False


# ──────────────────────────────────────────────
#  LOOP PRINCIPAL
# ──────────────────────────────────────────────
def main():
    conectar_wifi()

    cliente = None
    t_ping  = time.time()
    t_dht   = 0
    t_pub   = 0

    while True:
        try:
            # Reconexão MQTT se necessário
            if cliente is None:
                cliente = conectar_mqtt()
                t_ping  = time.time()

            agora = time.time()

            # ── 1. Leitura do DHT22 (temperatura + umidade do ar) ──
            if agora - t_dht >= CICLO_DHT_SEG:
                try:
                    sensor_dht.measure()
                    estado["temp_ar"]  = float(sensor_dht.temperature())
                    estado["umid_ar"]  = float(sensor_dht.humidity())
                except Exception as e:
                    if estado["temp_ar"] is None:  estado["temp_ar"]  = 24.0
                    if estado["umid_ar"] is None:  estado["umid_ar"]  = 65.0
                    print("[DHT22] Erro:", e)
                t_dht = agora

            # ── 2. Leitura do solo e pluviômetro ──
            verificar_pluviometro()
            estado["umid_solo"] = ler_umidade_solo()

            # ── 3. Aplicar lógica de controle ──
            aplicar_logica()

            # ── 4. Publicar telemetria via MQTT ──
            if agora - t_pub >= CICLO_MQTT_SEG:
                temp  = estado["temp_ar"]
                umid  = estado["umid_ar"]
                solo  = estado["umid_solo"]
                chuva = chuva_mm_24h()
                nomes_modo = {MODO_AUTO: "AUTO", MODO_ON: "ON", MODO_OFF: "OFF"}
                json_estado = json.dumps({
                    "modo":      nomes_modo[estado["modo"]],
                    "bomba":     estado["bomba"],
                    "hora":      hora_atual(),
                    "bloqueado": estado["bloqueado"],
                    "chuva_mm":  chuva,
                    "umid_solo": solo,
                })

                cliente.publish(TOPIC_TEMP,   str(temp))
                cliente.publish(TOPIC_HUM,    str(umid))
                cliente.publish(TOPIC_SOLO,   str(solo))
                cliente.publish(TOPIC_CHUVA,  str(chuva))
                cliente.publish(TOPIC_ESTADO, json_estado)
                cliente.publish(TOPIC_ALARME,
                                "BLOQUEADO" if estado["bloqueado"] else "OK")

                _cache_mqtt.update({"temp": temp, "umid": umid,
                                    "solo": solo, "chuva": chuva,
                                    "estado": json_estado})
                t_pub = agora

            # ── 5. Receber mensagens MQTT + keepalive ──
            cliente.check_msg()
            if agora - t_ping >= CICLO_KEEPALIVE:
                cliente.ping()
                t_ping = agora

            time.sleep(0.05)

        except Exception as e:
            import sys
            sys.print_exception(e)
            bomba.off();  estado["bomba"]     = False
            alarme.off(); estado["bloqueado"] = False
            print("[Sistema] Reiniciando conexão em 3 s...")
            try:
                if cliente:
                    cliente.disconnect()
            except Exception:
                pass
            cliente = None
            time.sleep(3)


if __name__ == "__main__":
    main()
