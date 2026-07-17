"""
Motor 3 — CLV (Closing Line Value).

Escanea la cartera ACTIVA y, a T-30 min del cierre del mercado, liquida las posiciones
direccionales (las que dejó abiertas el Motor 2) para capturar el closing line value:
la línea de Kalshi tiende a converger al consenso cerca del cierre, así que salir ahí
realiza el edge antes de la resolución y reduce la varianza de aguantar hasta el final.

Componentes (read-only → shadow → ejecución, detrás de MOTOR_3_CLV_ENABLED):
  - poller:   sincroniza GET /portfolio/positions → tabla PortfolioPosition (+ close_time).
  - detector: lógica pura de ventana de salida (close_time − now ∈ [28, 30] min).
  - executor: SELL del lado abierto al bid (liquidación), reusando el sensor FOK/409.
  - engine:   bucle supervisado que ensambla los tres.
"""
