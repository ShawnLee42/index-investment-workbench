#!/bin/bash
# 一键部署脚本：创建仓库 + 推送文件 + 触发 Actions
# 用法：在本地有 gh CLI 的环境中运行 bash setup.sh

set -e

REPO_NAME="index-investment-workbench"
REPO_DESC="指数投资工作台 - 沪深300等指数历史行情与估值数据"

echo "=== 1. 创建 GitHub 仓库 ==="
if gh repo view "$REPO_NAME" >/dev/null 2>&1; then
    echo "  仓库已存在，跳过创建"
else
    gh repo create "$REPO_NAME" --public --description "$REPO_DESC"
    echo "  ✓ 仓库已创建"
fi

echo ""
echo "=== 2. 推送文件 ==="
git remote remove origin 2>/dev/null || true
git remote add origin "https://github.com/$(gh api user --jq .login)/$REPO_NAME.git"
git push -u origin main
echo "  ✓ 文件已推送"

echo ""
echo "=== 3. 触发首次数据初始化 ==="
gh workflow run weekly-update.yml --repo "$REPO_NAME" 2>/dev/null || true
echo "  ✓ Actions 已触发（或将在推送后自动触发）"

echo ""
echo "=== 完成 ==="
echo "  仓库地址: https://github.com/$(gh api user --jq .login)/$REPO_NAME"
echo "  Actions: https://github.com/$(gh api user --jq .login)/$REPO_NAME/actions"
echo ""
echo "  首次运行会全量获取沪深300历史数据（2010年至今），约需2-3分钟。"
echo "  后续每周六 16:00 自动增量更新。"
