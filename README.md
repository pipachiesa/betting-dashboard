# Betting Dashboard

Dashboard de tendencias de fútbol, orientado a comparar líneas de apuestas con rendimientos partido por partido.

Ligas configuradas: Liga Profesional, Brasileirão, MLS, Premier League, LaLiga, Serie A y Ligue 1.

## Métricas

- Jugadores: remates, remates al arco, paradas de arqueros, tarjetas, faltas recibidas, faltas cometidas y entradas.
- Equipos: remates, remates al arco, córners, goles, goles recibidos, tarjetas propias/del rival, faltas cometidas/recibidas y entradas.
- Filtros: país, competencia, club, jugador, localía, últimos 5/10/15/20 y línea configurable.

## Actualización

`scripts/update_data.py` conserva los datos existentes y descarga el detalle únicamente de partidos nuevos o pendientes de migrar al esquema actual. GitHub Actions lo ejecuta todos los días a las 06:17 de Argentina y también permite una ejecución manual.

Brasileirão y MLS usan una carga histórica gradual de hasta 40 partidos únicos por ejecución. Las ligas europeas 2026/27 ya están configuradas y empiezan a recolectar desde su primera fecha; no mezclan encuentros de la temporada anterior.

Los datos provienen de endpoints públicos utilizados por FotMob y pueden requerir mantenimiento si el proveedor cambia su estructura. Los partidos sin cobertura detallada se representan como datos faltantes, nunca como cero.

Las métricas de tarjetas cuentan amarillas y rojas mostradas. La liquidación exacta de una apuesta puede variar según las reglas de cada casa.

## Desarrollo local

```bash
python -m http.server 8000
```

Abrir `http://localhost:8000`.
