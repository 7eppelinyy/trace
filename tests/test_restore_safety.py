from pathlib import Path
import pytest
from trace.db.backup import create_backup, restore_backup, verify_database_integrity
from scripts.restore_drill import run_drill


def test_fk_failure_is_not_a_successful_drill(db, tmp_path):
    db.execute('PRAGMA foreign_keys=OFF')
    db.execute("INSERT INTO research_question(question_id,user_id,title,hypothesis,created_at,updated_at) VALUES ('bad','missing','bad','bad','2026-01-01','2026-01-01')")
    db.execute('PRAGMA foreign_keys=ON')
    with pytest.raises(ValueError, match='validation failed'):
        run_drill(Path(db.path), tmp_path/'backups',tmp_path/'restore.db')


def test_all_business_tables_hashed_and_restore_does_not_overwrite(db, tmp_path):
    from trace.db.repositories import UserRepo
    UserRepo(db).ensure('usr_restore')
    db.execute("INSERT INTO research_question(question_id,user_id,title,hypothesis,created_at,updated_at) VALUES ('private','usr_restore','title','hypothesis','2026-01-01','2026-01-01')")
    backup = create_backup(db.path,tmp_path/'backups')
    checks = verify_database_integrity(backup)
    assert checks['table_counts']['research_question'] == 1
    assert {'alert_outbox','processing_job','user_session','research_share'} <= checks['table_hashes'].keys()
    result = restore_backup(backup,tmp_path/'restored.db')
    assert result['verification']['table_hashes'] == checks['table_hashes']
    with pytest.raises(ValueError, match='new target'):
        restore_backup(backup,db.path)


def test_missing_source_never_creates_seeded_pass(tmp_path):
    missing = tmp_path/'does-not-exist.db'
    with pytest.raises(FileNotFoundError):
        run_drill(missing,tmp_path/'backups',tmp_path/'restored.db')
    assert not missing.exists()
