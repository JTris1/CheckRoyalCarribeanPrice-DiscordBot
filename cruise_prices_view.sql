-- Gives the Discord bot the five columns it reads, from the tracker's own price_points table.
-- (After the Discord export has been backfilled into price_points, this is the only thing the
-- bot needs; public.royal_carribean is no longer used.)
CREATE OR REPLACE VIEW public.cruise_prices AS
SELECT cabin_label                                                       AS room_type,
       to_char(to_date(sail_date, 'YYYYMMDD'), 'MM/DD/YY') || ' ' || ship_name AS cruise_description,
       current_price                                                     AS price,
       observed_at                                                       AS datetime,
       NULL::text                                                        AS url
FROM public.price_points
WHERE item_kind = 'cabin_fare'
  AND status = 'priced'
  AND current_price IS NOT NULL
  AND cabin_label IS NOT NULL
  AND ship_name IS NOT NULL;

-- The bot's read-only user only needs the view (a view runs with its owner's rights):
-- GRANT SELECT ON public.cruise_prices TO bot_user;
