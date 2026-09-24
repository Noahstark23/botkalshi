-- DDL compiled from src/storage/models.py (SQLModel metadata). DO NOT EDIT BY HAND.
-- Regenerate: see tests/strategies/test_production_schema_fixture.py (drift check).

CREATE TABLE trades (
	id INTEGER NOT NULL, 
	client_order_id VARCHAR(100) NOT NULL, 
	ticker VARCHAR(100) NOT NULL, 
	side VARCHAR(10) NOT NULL, 
	action VARCHAR(10) NOT NULL, 
	count INTEGER NOT NULL, 
	price_cents INTEGER NOT NULL, 
	strategy VARCHAR(50) NOT NULL, 
	estimated_edge_pct FLOAT, 
	kalshi_order_id VARCHAR, 
	status VARCHAR(20) NOT NULL, 
	fill_price_cents INTEGER, 
	fees_cents INTEGER, 
	pnl_cents INTEGER, 
	closed_by_clv BOOLEAN NOT NULL, 
	filled_count INTEGER, 
	placed_at DATETIME NOT NULL, 
	filled_at DATETIME, 
	settled_at DATETIME, 
	notes VARCHAR(500), 
	PRIMARY KEY (id), 
	UNIQUE (client_order_id)
);

CREATE TABLE operational_state (
	"key" VARCHAR(50) NOT NULL, 
	value VARCHAR(20) NOT NULL, 
	reason VARCHAR(500), 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY ("key")
);

CREATE TABLE risk_events (
	id INTEGER NOT NULL, 
	event_type VARCHAR(50) NOT NULL, 
	severity VARCHAR(20) NOT NULL, 
	message VARCHAR(1000) NOT NULL, 
	capital_at_event FLOAT, 
	triggered_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE portfolio_positions (
	id INTEGER NOT NULL, 
	ticker VARCHAR(100) NOT NULL, 
	side VARCHAR(10) NOT NULL, 
	count INTEGER NOT NULL, 
	exposure_cents INTEGER, 
	close_time DATETIME, 
	synced_at DATETIME NOT NULL, 
	peak_bid_cents INTEGER, 
	PRIMARY KEY (id)
);
