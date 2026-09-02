<script setup>
// 内容区底部悬浮操作条（Ant Pro FooterToolbar 风格）：滚到哪都能保存。
</script>

<template>
  <div class="footer-toolbar">
    <div class="ft-left"><slot name="left" /></div>
    <div class="ft-right"><slot /></div>
  </div>
</template>

<style scoped>
.footer-toolbar {
  position: sticky;
  bottom: 0;
  z-index: 10;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin: 16px -4px -4px;
  padding: 12px 20px;
  background: var(--app-header-bg);
  border: 1px solid var(--app-border);
  border-radius: var(--app-radius-lg);
  box-shadow: var(--app-shadow-lg);
}
.ft-left { color: var(--el-text-color-secondary); font-size: 12px; }
.ft-right { display: flex; align-items: center; gap: 10px; margin-left: auto; }
</style>
