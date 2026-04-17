❯  每次调用 GLM-4 API 时，tools 参数里会传入完整的     
  JSON                                                 
    Schema：  具体给出对应的代码在哪里                 
                                                       
⏺ 三处代码串联起来完成这件事：                         
                                                       
  第一步：MCP 服务器暴露工具定义              
  mcp_servers/client.py:83-87 — 启动时从每个 MCP server
   收集工具                                            
  tools_response = await session.list_tools()          
  for tool in tools_response.tools:          
      self._tools[tool.name] = {                       
          "schema": self._to_openai_schema(tool),  # 
  转成 OpenAI 格式                                     
          "server": server_name,                       
      }                                   
                                                       
  第二步：转换成 OpenAI function-call 格式             
  mcp_servers/client.py:137-147 — tool.inputSchema 就是
   MCP server 提供的 JSON Schema                       
  @staticmethod                           
  def _to_openai_schema(tool: Any) -> dict:            
      return {                                         
          "type": "function",
          "function": {                                
              "name": tool.name,              
              "description": tool.description or "",
              "parameters": tool.inputSchema if     
  tool.inputSchema else {...},                         
          },                                  
      }                                                
                  
  第三步：按阶段过滤后传给模型                         
  agent/loop.py:92 — 每次循环取当前阶段允许的工具列表
  tools = mcp.get_tools_for_phase(state_machine.current
  _phase.value)                                        
                                              
  mcp_servers/client.py:107-114 — 按                   
  PHASE_TOOL_PERMISSIONS 过滤                 
  def get_tools_for_phase(self, phase: str) ->         
  list[dict]:                                 
      allowed = PHASE_TOOL_PERMISSIONS.get(phase, [])  
      tools = [TRANSITION_TOOL]  # 始终包含          
      for name in allowed:                             
          if name in self._tools:                      
              tools.append(self._tools[name]["schema"])
    # 这里是完整 schema                                
      return tools                                     
                                          
  第四步：传入 API 调用                                
  agent/loop.py:110-117                                
  response = client.chat.completions.create(  
      model=cfg.model,                                 
      messages=api_messages,                           
      tools=tools,          # ← 就是这里，完整 JSON    
  Schema 列表                                          
      tool_choice="required",                          
      max_tokens=cfg.max_tokens,          
  )
                                                       
  ---                                     
  完整链路：                                           
                                                       
  MCP server list_tools()
      → _to_openai_schema()         # client.py:137    
      → self._tools[name]["schema"] # client.py:84
      → get_tools_for_phase()       # client.py:107
      → tools 变量                  # loop.py:92
      → client.chat.completions.create(tools=tools)  # 
  loop.py:114                                          
                                                       
  每次循环都会执行一遍这个链路，确保模型在不同阶段看到 
  不同的工具集。 