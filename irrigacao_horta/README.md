# 🌿 Sistema de Irrigação Inteligente para Horta Urbana
### ESP32 + MicroPython + MQTT

> Projeto acadêmico — Sistemas Embarcados e IoT  
> Simulação no Wokwi | Protocolo MQTT via HiveMQ público

---

## Sumário

1. [Descrição do Projeto](#descrição-do-projeto)
2. [Arquitetura do Sistema](#arquitetura-do-sistema)
3. [Hardware Utilizado](#hardware-utilizado)
4. [Protocolo MQTT — Tópicos e Payloads](#protocolo-mqtt--tópicos-e-payloads)
5. [Lógica de Controle Automático](#lógica-de-controle-automático)
6. [Estrutura do Repositório](#estrutura-do-repositório)
7. [Como Executar a Simulação](#como-executar-a-simulação)
8. [Exemplos de Dados Capturados](#exemplos-de-dados-capturados)

---

## Descrição do Projeto

Este projeto implementa um sistema embarcado de **irrigação automática para horta urbana**, utilizando um microcontrolador **ESP32 DevKitC V4** programado em **MicroPython**. A comunicação entre o dispositivo e sistemas externos é realizada pelo protocolo de mensageria leve **MQTT** (broker público HiveMQ), tornando o sistema monitorável e controlável remotamente em tempo real.

O diferencial em relação a sistemas convencionais está na **tomada de decisão baseada em múltiplos sensores**:

- **Umidade do solo** — sensor capacitivo (simulado por potenciômetro no Wokwi)
- **Temperatura e umidade do ar** — sensor DHT22
- **Precipitação pluviométrica** — pluviômetro de báscula (simulado por push-button)
- **Hora do dia** — janela de irrigação restrita ao período de menor evaporação (06h–09h)

---

## Arquitetura do Sistema

```
┌──────────────────────────────────────────────────────┐
│               ESP32 DevKitC V4 (Wokwi)               │
│                                                      │
│  DHT22 ──► GPIO15    ADC Solo ──► GPIO34             │
│  Pluviômetro ──► GPIO4                               │
│  LED Bomba ◄── GPIO2    LED Alarme ◄── GPIO5         │
│                                                      │
│  ┌──────────────┐      ┌─────────────────────────┐   │
│  │  MicroPython │      │    umqtt.simple (MQTT)  │   │
│  │   v1.22.0    │─────►│  broker.hivemq.com:1883 │   │
│  └──────────────┘      └────────────┬────────────┘   │
└───────────────────────────────────┬─┘                │
                                    │ Wi-Fi
                         ┌──────────▼──────────┐
                         │   Broker HiveMQ     │
                         │  (público / sem TLS)│
                         └──────┬──────────────┘
                                │
               ┌────────────────┼─────────────────┐
               │                │                 │
       ┌───────▼──────┐  ┌──────▼──────┐  ┌──────▼──────┐
       │ MQTT Explorer│  │  Node-RED   │  │ Qualquer    │
       │ (monitorar) │  │  Dashboard  │  │ subscriber  │
       └──────────────┘  └─────────────┘  └─────────────┘
```

---

## Hardware Utilizado

| Componente | Pino ESP32 | Função |
|---|---|---|
| DHT22 | GPIO 15 | Temperatura e umidade do ar |
| Potenciômetro (ADC) | GPIO 34 | Simula sensor capacitivo de solo |
| Push-button | GPIO 4 | Simula pluviômetro de báscula |
| LED Verde | GPIO 2 | Indica bomba ligada |
| LED Vermelho | GPIO 5 | Indica bloqueio/alarme |
| UART0 (TX/RX) | Pinos padrão | Monitor serial |
| Logic Analyzer | D0=GPIO4, D1=GPIO2, D2=TX | Depuração de sinais |

---

## Protocolo MQTT — Tópicos e Payloads

### Tópicos Publicados pelo ESP32

| Tópico | Tipo | Exemplo de Payload |
|---|---|---|
| `horta/iot/sensor/temp_ar` | float | `24.5` |
| `horta/iot/sensor/umid_ar` | float | `65.3` |
| `horta/iot/sensor/umid_solo` | float | `38.7` |
| `horta/iot/sensor/chuva_24h` | float | `1.397` |
| `horta/iot/sistema/estado` | JSON | `{"modo":"AUTO","bomba":true,"hora":7,"bloqueado":false,"chuva_mm":0.0,"umid_solo":38.7}` |
| `horta/iot/sistema/alarme` | string | `OK` ou `BLOQUEADO` |

### Tópico Subscrito pelo ESP32 (Comandos)

**Tópico:** `horta/iot/sistema/cmd`

```json
// Alterar modo de operação
{ "mode": "AUTO" }
{ "mode": "ON" }
{ "mode": "OFF" }

// Forçar hora para testes (sem alterar o RTC)
{ "force_hour": 7 }

// Zerar contagem de chuva
{ "reset_chuva": true }
```

### Como monitorar os tópicos MQTT

- **[MQTT Explorer](https://mqtt-explorer.com/)** — conecte em `broker.hivemq.com:1883`
- **[HiveMQ WebSocket Client](https://www.hivemq.com/demos/websocket-client/)** — direto no browser
- **Node-RED** — crie um dashboard visual conectando-se ao mesmo broker

---

## Lógica de Controle Automático

A decisão de ligar ou desligar a bomba segue uma hierarquia de condições:

```
┌─────────────────────────────────────────────────┐
│  INÍCIO DO CICLO (a cada 50 ms)                 │
└────────────────────┬────────────────────────────┘
                     │
         ┌───────────▼───────────┐
         │  Modo = MANUAL ON?    │──YES──► Liga bomba ✓
         └───────────┬───────────┘
                     │ NO
         ┌───────────▼───────────┐
         │  Modo = MANUAL OFF?   │──YES──► Desliga bomba ✗
         └───────────┬───────────┘
                     │ NO (modo AUTO)
         ┌───────────▼───────────┐
         │  Chuva ≥ 3 mm/24h?   │──YES──► Desliga + Alarme 🔴
         └───────────┬───────────┘
                     │ NO
         ┌───────────▼───────────┐
         │  Hora fora 06h–09h?   │──YES──► Desliga + Alarme 🔴
         └───────────┬───────────┘
                     │ NO
         ┌───────────▼───────────┐
         │  Solo < 40% ?         │──YES──► Liga bomba 💧🟢
         └───────────┬───────────┘
                     │ NO
         ┌───────────▼───────────┐
         │  Solo ≥ 60% ?         │──YES──► Desliga bomba ✗
         └───────────┬───────────┘
                     │ NO (entre 40% e 60%)
                     ▼
              Mantém estado atual
```

**Parâmetros configuráveis** (em `main.py`):

| Constante | Valor padrão | Descrição |
|---|---|---|
| `SOLO_LIGAR` | 40.0 % | Aciona irrigação abaixo deste valor |
| `SOLO_DESLIGAR` | 60.0 % | Encerra irrigação acima deste valor |
| `CHUVA_BLOQ_MM` | 3.0 mm | Bloqueia se precipitação nas 24 h atingir |
| `HORA_INICIO` | 6 | Início da janela de irrigação (hora) |
| `HORA_FIM` | 9 | Fim da janela de irrigação (hora exclusive) |

---

## Estrutura do Repositório

```
irrigacao_horta/
├── main.py              ← Firmware principal (MicroPython)
├── diagram.json         ← Circuito do Wokwi (DevKitC V4)
├── README.md            ← Este documento
└── dados/
    ├── dados_telemetria.csv  ← 300 leituras dos sensores
    ├── mqtt_log.txt          ← Mensagens capturadas no broker
    └── serial_log.txt        ← Saída do Monitor Serial do Wokwi
```

---

## Como Executar a Simulação

1. Acesse [wokwi.com](https://wokwi.com) e crie um novo projeto **ESP32 DevKitC V4**.
2. Selecione firmware **MicroPython v1.22.0 (2023-12-27)**.
3. Cole o conteúdo de `main.py` no editor de código.
4. Cole o conteúdo de `diagram.json` na aba de diagrama.
5. Clique em **▶ Start Simulation**.
6. Abra o **Monitor Serial** para acompanhar os logs.
7. Para monitorar os tópicos MQTT em tempo real, use:
   - [MQTT Explorer](https://mqtt-explorer.com/) — conecte em `broker.hivemq.com:1883`
   - [HiveMQ WebSocket Client](https://www.hivemq.com/demos/websocket-client/) — via browser

### Enviando comandos remotos

Com qualquer cliente MQTT conectado ao broker, publique no tópico `horta/iot/sistema/cmd`:

```bash
# Ligar bomba manualmente
mosquitto_pub -h broker.hivemq.com -t horta/iot/sistema/cmd -m '{"mode":"ON"}'

# Retornar ao automático
mosquitto_pub -h broker.hivemq.com -t horta/iot/sistema/cmd -m '{"mode":"AUTO"}'

# Simular horário de irrigação para testes
mosquitto_pub -h broker.hivemq.com -t horta/iot/sistema/cmd -m '{"force_hour":7}'
```

---

## Exemplos de Dados Capturados

Payload típico do tópico `horta/iot/sistema/estado` durante operação normal:

```json
{
  "modo": "AUTO",
  "bomba": true,
  "hora": 7,
  "bloqueado": false,
  "chuva_mm": 0.0,
  "umid_solo": 35.2
}
```

Payload durante bloqueio por chuva excessiva:

```json
{
  "modo": "AUTO",
  "bomba": false,
  "hora": 7,
  "bloqueado": true,
  "chuva_mm": 4.189,
  "umid_solo": 22.4
}
```

---

## Dados Coletados

A pasta `dados/` contém as evidências reais da simulação executada no Wokwi em 2026-05-26 às 06h00, com duração de 10 minutos (300 ciclos de 2 segundos).

| Arquivo | Descrição |
|---|---|
| `dados_telemetria.csv` | 300 leituras de todos os sensores + estado da bomba e alarme |
| `mqtt_log.txt` | Mensagens capturadas no broker HiveMQ via MQTT Explorer |
| `serial_log.txt` | Saída completa do Monitor Serial do Wokwi |

### Eventos registrados durante a sessão

| Horário | Evento |
|---|---|
| 06:00:00 | Boot do ESP32, conexão Wi-Fi e MQTT estabelecida |
| 06:01:00 | Solo cai abaixo de 40% → bomba ligada automaticamente |
| 06:01:02 | 1ª basculada do pluviômetro (0,279 mm) |
| 06:01:11 | 2ª basculada do pluviômetro (0,559 mm) |
| 06:02:00 | Solo sobe acima de 60% → bomba desligada automaticamente |
| 06:03:00 | Comando MQTT `{"mode":"ON"}` → bomba ligada manualmente |
| 06:04:00 | Comando MQTT `{"mode":"AUTO"}` → retorno ao automático |
| 06:05:00 | Comando `{"force_hour":12}` → bloqueio por horário ativado |
| 06:06:00 | Comando `{"force_hour":7}` → bloqueio removido |
| 06:09:58 | Última leitura — fim da simulação |

---

*Projeto desenvolvido como trabalho acadêmico na disciplina de Objetos Inteligentes Conectados / IoT.*
