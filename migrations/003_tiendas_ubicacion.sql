-- ============================================================
-- MIGRACIÓN 003 — Ubicación por tienda (asistencia Cadenas)
-- YA EJECUTADA manualmente en Supabase → SQL Editor (2026-11-03).
-- Se guarda solo como registro; no la ejecuta la app.
-- ============================================================

-- La validación de asistencia de promotores de Cadenas mide contra
-- la tienda del promotor (usuarios.tienda_id). Si la tienda no tiene
-- coordenadas, se usan latitud_promotor / longitud_promotor del usuario.
ALTER TABLE tiendas
ADD COLUMN IF NOT EXISTS latitud double precision,
ADD COLUMN IF NOT EXISTS longitud double precision,
ADD COLUMN IF NOT EXISTS radio_metros integer DEFAULT 100;
