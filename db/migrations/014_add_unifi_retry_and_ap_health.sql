-- Client-side wireless retry rate and link quality (the wireless
-- equivalent of the wired-side interface error/collision metrics) --
-- signal/satisfaction alone don't show RF congestion or degraded PHY
-- rates the way retry counts and negotiated tx/rx rate do.
ALTER TABLE client_metrics ADD COLUMN IF NOT EXISTS tx_retries BIGINT;
ALTER TABLE client_metrics ADD COLUMN IF NOT EXISTS wifi_tx_attempts BIGINT;
ALTER TABLE client_metrics ADD COLUMN IF NOT EXISTS tx_rate BIGINT;
ALTER TABLE client_metrics ADD COLUMN IF NOT EXISTS rx_rate BIGINT;
ALTER TABLE client_metrics ADD COLUMN IF NOT EXISTS noise INT;
ALTER TABLE client_metrics ADD COLUMN IF NOT EXISTS channel INT;
ALTER TABLE client_metrics ADD COLUMN IF NOT EXISTS essid TEXT;
ALTER TABLE client_metrics ADD COLUMN IF NOT EXISTS is_wired BOOLEAN;

-- AP-level state/health -- state gives direct AP up/down detection (the
-- wireless equivalent of the Router Status online/offline panel) instead
-- of inferring it from an AP silently vanishing from client data.
ALTER TABLE ap_inventory ADD COLUMN IF NOT EXISTS state INT;
ALTER TABLE ap_inventory ADD COLUMN IF NOT EXISTS satisfaction INT;
ALTER TABLE ap_inventory ADD COLUMN IF NOT EXISTS num_sta INT;
ALTER TABLE ap_inventory ADD COLUMN IF NOT EXISTS uptime BIGINT;
