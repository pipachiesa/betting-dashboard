# Betting Dashboard

Dashboard de fútbol con dos capas: **tendencias** partido por partido y un **modelo predictivo**
que estima la probabilidad de cada mercado.

Ligas: Liga Profesional, Brasileirão, MLS, Premier League, LaLiga, Serie A y Ligue 1.

## Las tres capas de datos

| Archivo | Rol | Se regenera |
|---|---|---|
| `data.json` | Ventana móvil de 60 partidos por equipo, para los gráficos | Diario |
| `history/` | Archivo append-only, una fila por (partido, equipo). Es el set de entrenamiento | Diario (append) + backfill |
| `model.json` | Coeficientes ajustados que el navegador evalúa | Diario |

La separación importa: `data.json` es una foto, no un historial. El modelo no puede entrenarse
sobre él porque solo guarda la temporada en curso.

## Métricas

Cada fila de `history/teams/` trae **lo producido y lo concedido** para cada estadística
(`shots` / `shots_ag`, `corners` / `corners_ag`, …), porque un modelo de ataque/defensa necesita
las dos mitades. También incluye:

- **xG**: total, jugada abierta, balón parado, sin penales, y xGOT — propio y en contra.
- **Zonas de remate**: cada tiro del shotmap se clasifica sobre una cancha de 105x68 metros en
  `sixyard`, `box_c`, `box_w`, `out_c`, `out_w`, con conteo y xG por zona, propio y concedido.
- **Situación**: jugada abierta, contraataque, córner, tiro libre, penal.
- Árbitro, posesión, ocasiones claras, toques en el área, entradas, intercepciones, despejes, duelos.

`history/players/` guarda 49 columnas por jugador-partido, incluido `xgot_faced` de los arqueros,
que permite modelar atajadas por calidad de remate y no solo por cantidad.

⚠️ **Liga Profesional no tiene xG ni shotmap en la fuente.** Para Argentina esas columnas vienen
vacías y hay que apoyarse en ocasiones claras y remates dentro del área.

## Scripts

```bash
# backfill histórico: 5 temporadas completas + la actual, de las 7 ligas
python3 scripts/backfill.py --seasons 5

# reconstruir los CSV desde la caché, sin volver a descargar
python3 scripts/backfill.py --seasons 5 --reparse

# ajustar el modelo -> model.json
python3 scripts/build_model.py

# backtest walk-forward contra dos baselines
python3 scripts/backtest.py
```

El backfill es **resumible**: `scripts/fotmob.py` guarda cada payload podado y comprimido en
disco, y esa caché es a la vez el registro de progreso. Se puede matar y relanzar sin perder
trabajo. La caché vive fuera del repo (`FOTMOB_CACHE`, por defecto un directorio temporal);
son cientos de MB y nunca debe llegar a git.

## El modelo

Conteos de equipo con estructura multiplicativa:

```
lambda(A vs B) = mu_liga * ataque_A * defensa_B * local^(+/-1)
```

Ataque y defensa se resuelven por ajuste proporcional iterativo, así un equipo no se lleva
crédito por acumular remates contra defensas débiles. Ambos factores se contraen hacia 1 con
`k` partidos ficticios, y los partidos se ponderan con decaimiento exponencial.

Los conteos están sobredispersos respecto de Poisson (remates: varianza/media = 1.97), así que
la cola sale de una **binomial negativa** con dispersión ajustada por momentos sobre los
residuos. Las tarjetas son *sub*dispersas (0.86) y se dejan en Poisson.

Los props de jugador son una porción del total del equipo: la porción se estima por 90 minutos,
se contrae hacia un prior por posición y se multiplica por los minutos esperados. Las atajadas
del arquero se calculan sobre los remates al arco **del rival**, no sobre nada propio.

### Resultados del backtest

Walk-forward sobre las 7 ligas, 14.633 partidos, refit cada 14 días. Nada se ajusta con datos
posteriores al partido. Brier score agregado, ponderado por cantidad de predicciones:

| Mercado | Modelo | Media de liga | Mejora | Le gana al promedio del propio equipo |
|---|---|---|---|---|
| Remates | **0.1871** | 0.2173 | +13.9% | 7/7 ligas |
| Remates al arco | **0.1984** | 0.2160 | +8.1% | 7/7 |
| Faltas | **0.2012** | 0.2156 | +6.7% | 7/7 |
| Entradas | **0.1885** | 0.1999 | +5.7% | 7/7 |
| Córners | **0.2111** | 0.2235 | +5.6% | 7/7 |
| Goles | **0.1773** | 0.1858 | +4.6% | 7/7 |
| Tarjetas | **0.1891** | 0.1928 | +1.9% | 7/7 |

Las 49 combinaciones liga-mercado dan mejora positiva. Remates es el más predecible y se mantiene
entre +12% y +19% en las siete ligas. Las tarjetas son el más flojo (+1% a +3%): dependen mucho
del árbitro, que todavía no entra en el modelo aunque ya se guarda en `history/`.

La calibración de remates cae dentro de ±0.03 en los diez bins.

### Qué mueve los remates

Selección hacia adelante con R² fuera de muestra:

| Feature | Gana en R² | Efecto (+1 desvío) |
|---|---|---|
| Localía | +0.097 | **+1.55 remates** |
| Remates que concede el rival | +0.047 | +0.70 |
| Remates propios (últimos 10) | +0.034 | +0.90 |

La localía vale **+3.12 remates** (14.29 de local contra 11.17 de visitante, +24.5%), consistente
en todas las ligas. Es el predictor más fuerte del mercado.

Lo que concede el rival aporta más que el historial propio: sin las columnas `_ag` el modelo
tendría media ecuación, y es la mitad más informativa.

xG, toques en el área, remates dentro del área, córners y las zonas **no aportan nada
incremental** al volumen de remates: son aguas abajo del propio volumen. Sirven para mercados de
calidad (goles, xG), no de cantidad. El techo de R² es 0.184, o sea que el 82% de la varianza de
remates es ruido irreducible del partido.

Sobre xT: no es calculable con esta fuente (ver `scripts/tilt_experiment.py`).

**Advertencia honesta**: estas probabilidades son del modelo, no del mercado. Ganarle de forma
consistente a una línea de cierre en props de jugador es muy difícil. La utilidad realista es
detectar desacuerdos, no asumir que el modelo tiene razón.

## Actualización

GitHub Actions corre `update_data.py` todos los días a las 06:17 de Argentina: refresca el
snapshot, **agrega los partidos nuevos al archivo histórico** y reconstruye `model.json`.

Los datos vienen de endpoints públicos de FotMob y pueden requerir mantenimiento si el proveedor
cambia su estructura. Los partidos sin cobertura se representan como faltantes, nunca como cero.

## Desarrollo local

```bash
python3 -m http.server 8000
```
