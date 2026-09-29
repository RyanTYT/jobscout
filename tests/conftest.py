"""tests/conftest.py — the shared webapp-test fixtures.

One SEED, one throwaway DB builder, one TestClient factory (with the
config-dir/env patch points pre-wired). Test files stay focused on
behavior.
"""

from __future__ import annotations

import sqlite3
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobscout.core import db
from jobscout.webapp.routes import create_app

SEED = textwrap.dedent("""
INSERT INTO companies (id, name, domain, tier, non_ats, career_url)
VALUES
  ('acme',  'Acme',    'acme.com',  'A', 0, NULL),
  ('globex', 'Globex', 'globex.com', 'B', 1, 'https://globex.com/careers');

WITH RECURSIVE seq(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM seq WHERE i < 60)
INSERT INTO postings (id, source, company_id, url, url_hash, title,
                      location, seniority, remote, rule_pass, status,
                      first_seen, last_seen, content_hash)
SELECT
  'p_new_' || printf('%03d', i),
  CASE WHEN i % 2 = 0 THEN 'ats:greenhouse:acme' ELSE 'careers:jsonld:globex' END,
  CASE WHEN i % 2 = 0 THEN 'acme' ELSE 'globex' END,
  'https://x/' || i, printf('%064d', i),
  'Software Engineer ' || i,
  CASE WHEN i % 3 = 0 THEN 'New York, NY' ELSE 'Singapore' END,
  CASE WHEN i % 4 = 0 THEN 'senior' ELSE NULL END,
  CASE WHEN i % 5 = 0 THEN 1 ELSE 0 END,
  1, 'new', '2026-09-28T00:00:00Z', '2026-09-28T00:00:00Z', printf('%064x', i)
FROM seq;

INSERT INTO postings (id, source, company_id, url, url_hash, title,
                      location, seniority, rule_pass, status, first_seen,
                      last_seen, content_hash)
VALUES
  ('p_int_1', 'ats:greenhouse:acme', 'acme', 'https://x/int1',
   'i1', 'Senior Quant', 'New York, NY', 'senior', 1, 'interested',
   '2026-09-27T00:00:00Z', '2026-09-27T00:00:00Z', 'i1x'),
  ('p_dis_1', 'ats:greenhouse:acme', 'acme', 'https://x/dis1',
   'd1', 'Junior Analyst', 'London', 'junior', 1, 'dismissed',
   '2026-09-27T00:00:00Z', '2026-09-27T00:00:00Z', 'd1x'),
  ('p_rule_1', 'ats:greenhouse:acme', 'acme', 'https://x/rule1',
   'r1', 'Chef', 'Berlin', NULL, 0, 'new', '2026-09-27T00:00:00Z',
   '2026-09-27T00:00:00Z', 'r1x');
UPDATE companies SET ats_tokens = '{"greenhouse":"acme"}' WHERE id = 'acme';
""")

PACKET_SEED = textwrap.dedent("""
INSERT INTO packets (id, posting_id, status, dir)
VALUES ('pk_w1', 'p_int_1', 'packet:ready', '{pkt_dir}');
""")


def make_db_file(tmp_path: Path) -> Path:
    path = tmp_path / "test.db"
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA)
    db._migrate(conn)
    conn.executescript(SEED)
    conn.commit()
    conn.close()
    return path


def _seed_packet(db_file: Path, tmp_path: Path) -> Path:
    pkt_dir = tmp_path / "pkt"
    pkt_dir.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_file)
    conn.executescript(PACKET_SEED.format(pkt_dir=pkt_dir))
    conn.commit()
    conn.close()
    return pkt_dir


def make_client(db_file: Path) -> TestClient:
    def fake_connect():
        c = sqlite3.connect(db_file)
        c.row_factory = sqlite3.Row
        return c

    import jobscout.core.db as _db

    _db.connect = fake_connect          # patch the package attribute
    _db.init_db = lambda: db_file
    return TestClient(create_app())


@pytest.fixture()
def db_file(tmp_path):
    return make_db_file(tmp_path)


@pytest.fixture()
def client(monkeypatch, db_file):
    # single provider: tests that relocate config patch paths.config_dir
    return make_client(db_file)
