"""TimeAudit hardware series consumed by the website's background collector."""

CHANNELS = (
    ("disk", "磁盘", "最忙磁盘的忙碌率", "percent", "disk_busy_pct",
     "各物理盘活跃时间占比，取每次采样中最忙的一块"),
    ("memory-swap", "内存", "页面文件占用率", "percent", "pagefile_usage_pct",
     "Windows 页面文件已用空间占比"),
    ("disk-temp", "电源和温度", "磁盘最高温度", "celsius", "disk_temperature_c",
     "各盘主温度，取每次采样中读到的最高值"),
)


def panels():
    result = {}
    for index, (identity, group, label, unit, column, basis) in enumerate(CHANNELS, 30):
        sql = f'''SELECT $__timeGroupAlias("timestamp", $__interval),
            AVG({column}) AS "{label}"
            FROM public.fact_system_hardware
            WHERE $__timeFilter("timestamp") AND {column} IS NOT NULL
            GROUP BY 1 ORDER BY 1'''
        result[f'native{index}'] = dict(id=index, group=group, title=label, series_id=identity, basis=basis,
                                       fieldConfig=dict(defaults=dict(unit=unit)), targets=[dict(rawSql=sql)])
    return result
