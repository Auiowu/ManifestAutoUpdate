# ManifestAutoUpdate
一个steam清单缓存仓库

## 项目简介

* 使用`Actions`自动爬取`Steam`游戏清单

##如何部署

1. fork本仓库
2. 初始化
    * 第一次运行程序会进行初始化操作
    * 初始化会生成`data`分支,使用`worktree`签出到`data`目录
    * 生成密钥用于加密`users.json`
        * 密钥生成路径位于: `data/KEY`
        * 同时程序会输出密钥的十六进制字符串,需要将其存放到github仓库密钥,名称保存为`KEY`
            * 打开你的仓库 -> `Settings` -> `Secrets` -> `Actions` -> `New repository secret`
            * 或者在你的仓库地址后面加上`/settings/secrets/actions/new`
