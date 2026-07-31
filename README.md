# Betting Dashboard

Dashboard de tendencias para la Liga Profesional argentina, orientado a comparar líneas de apuestas con rendimientos partido por partido.

## Métricas

- Jugadores: remates, remates al arco y paradas de arqueros.
- Equipos: remates, remates al arco, córners, goles y goles recibidos.
- Filtros: club, jugador, localía, últimos 5/10/15/20 y línea configurable.

## Actualización

`scripts/update_data.py` consulta los últimos 20 partidos finalizados de cada club y reconstruye `data.json`. GitHub Actions lo ejecuta todos los días a las 06:17 de Argentina y también permite una ejecución manual.

Los datos provienen de endpoints públicos utilizados por FotMob y pueden requerir mantenimiento si el proveedor cambia su estructura. Los partidos sin cobertura detallada se representan como datos faltantes, nunca como cero.

## Desarrollo local

```bash
python -m http.server 8000
```

Abrir `http://localhost:8000`.
