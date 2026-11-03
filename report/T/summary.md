# Block T result

**Verdict under H5: supported**

| View | Group | k/n | Share | 95% Clopper-Pearson |
|---|---|---:|---:|---|
| primary | control | 46/90 | 0.51 | 0.40–0.62 |
| primary | relayer | 46/74 | 0.62 | 0.50–0.73 |
| primary | treated | 90/90 | 1.00 | 0.96–1.00 |
| window_wide | control | 46/90 | 0.51 | 0.40–0.62 |
| window_wide | relayer | 46/74 | 0.62 | 0.50–0.73 |
| window_wide | treated | 90/90 | 1.00 | 0.96–1.00 |
| lead_150ms | control | 46/90 | 0.51 | 0.40–0.62 |
| lead_150ms | relayer | 46/74 | 0.62 | 0.50–0.73 |
| lead_150ms | treated | 90/90 | 1.00 | 0.96–1.00 |
| gap_10ms | control | 46/90 | 0.51 | 0.40–0.62 |
| gap_10ms | relayer | 46/74 | 0.62 | 0.50–0.73 |
| gap_10ms | treated | 90/90 | 1.00 | 0.96–1.00 |
| settled_link | control | 46/90 | 0.51 | 0.40–0.62 |
| settled_link | relayer | 22/28 | 0.79 | 0.59–0.92 |
| settled_link | treated | 90/90 | 1.00 | 0.96–1.00 |

- primary: Fisher, treated above control p = 4.9e-17, two-sided p = 9.8e-17
- window_wide: Fisher, treated above control p = 4.9e-17, two-sided p = 9.8e-17
- lead_150ms: Fisher, treated above control p = 4.9e-17, two-sided p = 9.8e-17
- gap_10ms: Fisher, treated above control p = 4.9e-17, two-sided p = 9.8e-17
- settled_link: Fisher, treated above control p = 4.9e-17, two-sided p = 9.8e-17
- Worst-case selection: treated 90/90, control 46/90, p = 4.9e-17

## By validator

| Validator | Group | Version | k/n | 95% | Classes |
|---|---|---|---:|---|---|
| `3SkE34PV` | control | 4.3.2 | 15/30 | 0.31–0.69 | same_slot 30 |
| `5pPRHnie` | relayer | 4.3.2 | 10/19 | 0.29–0.76 | none_landed 11, same_slot 19 |
| `8gDfqHXZ` | control | 4.3.2 | 11/30 | 0.20–0.56 | same_slot 30 |
| `E1r4Psq8` | treated | 4.4.0 | 30/30 | 0.88–1.00 | same_slot 30 |
| `EvnRmnMr` | relayer | 4.3.2 | 22/28 | 0.59–0.92 | outside_window 2, same_slot 28 |
| `GnC339vk` | relayer | 4.3.2 | 14/27 | 0.32–0.71 | none_landed 3, same_slot 27 |
| `HEL1USMZ` | treated | 4.4.0 | 30/30 | 0.88–1.00 | same_slot 30 |
| `bitHUmbK` | treated | 4.4.0 | 30/30 | 0.88–1.00 | same_slot 30 |
| `privaEdS` | control | 4.3.2 | 20/30 | 0.47–0.83 | same_slot 30 |

## Exclusions

- control: total 90, classes {'same_slot': 90}, not sent {}
- relayer: total 90, classes {'none_landed': 14, 'same_slot': 74, 'outside_window': 2}, not sent {}
- treated: total 90, classes {'same_slot': 90}, not sent {}
- Gap control: median 9.818 ms, range 9.579–10.061
- Gap relayer: median 10.007 ms, range 9.573–10.161
- Gap treated: median 9.818 ms, range 9.582–10.095
- Pairs on a connection younger than 5 s, control: 0 of 90
- Pairs on a connection younger than 5 s, relayer: 46 of 74
- Pairs on a connection younger than 5 s, treated: 0 of 90
- Lead over the first shred control: median 1013.2 ms, range 627.5–1474.2
- Lead over the first shred relayer: median 997.8 ms, range 819.3–1336.7
- Lead over the first shred treated: median 1073.2 ms, range 753.5–1423.8

## Could the network have swapped a pair

| Address | RTT, ms: min / p50 / p99 / max | Close ping pairs | Slower by at least 10 ms | Upper 95% bound |
|---|---|---:|---:|---:|
| 188.42.129.244:11228 | 43.5 / 44.9 / 69.6 / 70.2 | 188 | 0 | 0.0194 |
| 198.13.140.73:11228 | 28.0 / 29.1 / 53.6 / 53.9 | 600 | 1 | 0.0093 |
| 185.189.44.170:12002 | 49.5 / 51.6 / 82.4 / 125.4 | 478 | 13 | 0.0461 |
| 64.130.42.75:8002 | 34.5 / 35.7 / 60.6 / 61.1 | 521 | 1 | 0.0106 |
| 64.130.57.215:8002 | 34.2 / 35.3 / 60.1 / 60.3 | 514 | 0 | 0.0072 |
| 116.126.141.28:9002 | 244.2 / 246.1 / 270.4 / 283.9 | 144 | 0 | 0.0253 |
| 67.213.121.155:8002 | 44.4 / 46.5 / 71.1 / 71.8 | 569 | 0 | 0.0065 |
| 23.109.158.124:11228 | 33.9 / 34.8 / 59.6 / 59.8 | 195 | 0 | 0.0187 |
| 64.130.40.170:8002 | 34.0 / 35.4 / 60.0 / 60.2 | 501 | 4 | 0.0203 |
