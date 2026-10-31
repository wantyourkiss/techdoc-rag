import pytest
from techdoc.fact_guard import contradiction


@pytest.mark.parametrize('source,claim',[
    ('Connection timeout is 45 seconds.','Connection timeout is 45 minutes.'),
    ('连接超时为45秒。','连接超时为45分钟。'),
    ('Remote access is not permitted.','Remote access is permitted.'),
    ('远程访问不允许。','远程访问允许。'),
    ('Remote access is permitted.','Remote access is not permitted.'),
    ('Restart only after the backup completes.','Restart immediately.'),
    ('Restart only after the backup completes.','Restart only before the backup completes.'),
    ('Restart only after the backup completes.','Restart only after the test completes.'),
    ('只有备份完成后才允许重启。','立即重启。'),
    ('Retries must be between 2 and 8.','Retries must be between 8 and 2.'),
    ('重试次数为2到8次。','重试次数为8到2次。'),
    ('Server timeout is 45 seconds. Backup timeout is 12 seconds.','Server timeout is 12 seconds.'),
    ('服务器超时为45秒。备份超时为12秒。','服务器超时为12秒。'),
    ('Restart only after the backup completes.','Restart only after the backup completes, or immediately if needed.'),
    ('Restart only after the backup completes.','Restart only after the backup completes. Alternatively, restart immediately.'),
    ('Restart only after the backup completes.','Restart only after the backup completes or the test completes.'),
    ('只有备份完成后才允许重启。','只有备份完成后才允许重启，或者需要时立即重启。'),
    ('只有备份完成后才允许重启。','只有备份完成后才允许重启。否则立即重启。'),
    ('Local access is permitted, but remote access is not permitted.','Remote access is permitted.'),
    ('本地访问允许，但远程访问不允许。','远程访问允许。'),
])
def test_explicit_contradictions_are_rejected(source,claim):
    assert contradiction(claim,source)


@pytest.mark.parametrize('source,claim',[
    ('The server connection timeout is 30 seconds.','服务器连接超时为30秒。'),
    ('Connection timeout is 45 seconds.','Connection timeout is 45 seconds.'),
    ('远程访问不允许。','远程访问不允许。'),
    ('Remote access is not permitted.','Remote access is not permitted.'),
    ('Restart only after the backup completes.','Restart only after the backup completes.'),
    ('Retries must be between 2 and 8.','Retries must be between 2 and 8.'),
    ('Server timeout is 45 seconds. Backup timeout is 12 seconds.','Backup timeout is 12 seconds.'),
    ('服务器超时为45秒。备份超时为12秒。','备份超时为12秒。'),
    ('Remote access is not permitted. Local access is permitted.','Local access is permitted.'),
    ('Backups require 30 minutes. Connection timeout is 30 seconds.','Connection timeout is 30 seconds.'),
    ('Vectors represent semantic meaning.','向量用于表达语义信息。'),
    ('Local access is permitted, but remote access is not permitted.','Local access is permitted.'),
    ('Local access is permitted, but remote access is not permitted.','Remote access is not permitted.'),
    ('本地访问允许，但远程访问不允许。','本地访问允许。'),
    ('本地访问允许，但远程访问不允许。','远程访问不允许。'),
    ('Restart only after the backup or the test completes.','Restart only after the backup or the test completes.'),
    ('Restart only after the backup or the test completes.','Restart only after the backup completes.'),
    ('Restart only after the backup completes.','Restart only after the backup and diagnostics complete.'),
])
def test_supported_facts_translation_and_unrelated_qualifiers_are_preserved(source,claim):
    assert contradiction(claim,source) is None
