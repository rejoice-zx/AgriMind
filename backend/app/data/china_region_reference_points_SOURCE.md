# 区域天气参考点来源

上游：[pamaforce/geojson](https://github.com/pamaforce/geojson)，固定提交
`3336f3e5fc7a276aa83f4dc0ea502c12c0cd5ac0`。获取日期：2026-10-12。
上游从阿里云地理数据服务整理行政区 GeoJSON；许可为 MIT，完整版权及许可保存在
`china_region_reference_points_LICENSE.txt`。

本文件是区域参考点，不是精确农场地址，也不保证最新行政边界。
从 `geojson_gcj02` 下省、市文件的区县 feature 提取 `properties.center`，仅保留
行政区编码及名称都与本项目 `china_regions_2023.json` 一致的条目。上游文件编码
包含 GB18030，导入脚本显式解码后统一输出 UTF-8。

原始 center 使用 GCJ-02。通过常用 GCJ 偏移模型的六次迭代逆变换得到近似 WGS84，
四舍五入至小数点后六位；小数位数不代表定位精度。不能直接使用上游
`geojson_wgs84` 的 center 属性，因为其转换脚本只转换 geometry 的 coordinates。

生成文件：`china_region_reference_points.json`，2789 个区县参考点。
现有选择器共 3056 个区县级节点，剩余 267 个节点没有匹配参考点，包含开发区等
特殊节点或版本不一致的地区。无匹配时明确提示手动填写实际 WGS84 经纬度，
不会拿所属市、其他同名区县或演示坐标代替。

SHA256：`051087b5efb419a36832c73efefe5d8c710453c43c239db5bbbc60f690bfb3d9`。

生成脚本：`backend/tools/import_region_reference_points.py`。该脚本只用于显式维护数据，
运行时不访问此仓库；脚本拒绝覆盖已有输出，更新快照需要先审核版本和数据差异。
查询优先使用本地快照及省市区上下文。仅未收录地点保留原有 Open-Meteo 地名查询，
结果仍按省市过滤，不能将模糊候选当成确定的农场地址。
