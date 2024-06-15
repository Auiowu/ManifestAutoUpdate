# ManifestAutoUpdate
一个steam清单缓存仓库

## 项目简介

* 使用`Actions`自动爬取`Steam`游戏清单

## 如何部署

1. fork本仓库
2. 初始化
    * 第一次运行程序会进行初始化操作
    * 初始化会生成`data`分支,使用`worktree`签出到`data`目录
    * 生成密钥用于加密`users.json`
        * 密钥生成路径位于: `data/KEY`
        * 同时程序会输出密钥的十六进制字符串,需要将其存放到github仓库密钥,名称保存为`KEY`
            * 打开你的仓库 -> `Settings` -> `Secrets` -> `Actions` -> `New repository secret`
            * 或者在你的仓库地址后面加上`/settings/secrets/actions/new`
    * 增加账号密码到`data/users.json`:
        * 之后如果需要使用`Actions`需要将其推送到远程仓库
            * 再次运行程序,程序结束时会自动推送到`data`分支
            * 手动推送步骤如下:
                1. `cd data`: 切换到`data`目录
                2. `git add -u`: 增加修改的内容
                3. `git commit -m "update"`: 提交修改
                4. `git push origin data`: 推送到远程`data`分支

## 操作流程

1. Actions初始化和运行
    * 配置`workflow`读写权限: 仓库 -> `Settings` -> `Actions` -> `General` -> `Workflow permissions`
      -> `Read and write permissions`
    * 仓库打开`Actions`选择对应的`Workflow`点击`Run workflow`选择好参数运行
        * `INIT`: 初始化
            * `users`: 账号,可指定多个,逗号分隔
            * `password`: 密码,可指定多个,逗号分隔
            * `ssfn`: [ssfn](https://ssfnbox.com/),需要提前上传该文件到`credential_location`目录,可指定多个,逗号分隔
            * `2fa`: [shared_secret](https://zhuanlan.zhihu.com/p/28257212),可指定多个,逗号分隔
            * `update`: 是否更新账号
            * `update_users`: 需要更新的账号
            * 第一次初始化后记得保存密钥到仓库密钥,不然下次运行会因为没有密钥而报错,然后记得删除本次`Workflow`运行结果,防止密钥泄露,或者使用本地初始化更安全
        * `Trigger Workflow`: 触发工作流
            * 流程`CI` -> `PR` -> `MERGE`
                * `PR`: 自动pr清单到指定仓库
                    * 由于Github 禁止Actions递归创建pr ,所以需要创建一个个人访问令牌保存到仓库密钥token
