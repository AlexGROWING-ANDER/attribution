# 启动Prompt候选r2：中文审核版

供阅读，不和英文Prompt重复发送。实际启动文本见prompt_case001_optimized.txt。

## 启动文本

对当前attempt原生使用gui-trajectory-attribution，加载其必需引用，按Skill规定的分析流程和归因规则执行。

任务身份：case001，Mobile-Agent-v3，Hard Intents，QQ音乐test_1；随机播放热门搜索中的一首歌，加入名为test的歌单。英文实际输入还绑定具体attempt、模板哈希及政策版本。

开始分析或提交前，读取submission_protocol.md、input_index.json、step_index.json、单步Schema和全局Schema。启动文件可分别调用并行读取，保证输出不截断。协议规定工具、逐Step提交和完成条件，Skill提供分析方法。

只使用当前工作区内的本次材料。最终result.json由调用方组装并严格校验。

## 另读的提交协议

以下是工程约定，不重复定义主要根因、责任模块和标签的判断方法。

- 原始证据只读，分析结果写在工作区；不执行手机任务，不访问网络、GT、历史预测、其他模型或个人技能
- 索引列出来源、步骤、日志页及图库。来源编号和图库序号本身不证明图片时序
- 每张图片单独通过原生view_image送达，重复图也保留；使用冻结缓存与原尺寸，不缩放、拼图或替换为OCR
- 文字文件逐文件完整读取，exec_command每次max_output_tokens至少32000；独立日志先读，本步日志在提交前读。独立读取可并行，看图每轮可并行4—6个独立调用
- 单步与全局字段按对应Schema填写，证据编号及来源类型由程序绑定。单步verdict是局部记录，最终归因仍按Skill完成
- 基础记录通过io.ps1提交，按数字顺序保存，一次只提交一步；可同时请求下一步日志，等保存成功、材料返回后再继续
- 全量材料与步骤齐全后，按Skill完成综合判断与收尾复核；重大疑点最多两批定向回查，不重新通读全轨迹
- 新直接观察可追加到revision_NNN.json；原步骤、verdict和证据前缀不变，并说明追加内容
- 全局结果通过global模式提交，不重复由调用方生成的evidence、process、coverage和完整result.json
- 最后运行一次基础check；被拒时允许一次针对修正和一次复查，保留原步骤。调用方完成后严格校验一次，不向模型反馈回修
- 文件及材料齐全、本地检查通过后仅回复DONE；否则说明阻塞。局部纠正和规范化另行记录

完整英文工具命令及约定见stepwise/protocols/submission_protocol.md。Skill v3、Schema与组装逻辑均未修改；候选尚未执行新模型测试。
