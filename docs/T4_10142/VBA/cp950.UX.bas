Attribute VB_Name = "UX"

' Control 元件是否與 Cell 連動
Public Function obj_ref_cel(ByRef obj As Object, ByRef cel As Object, ByVal ref_addr As Boolean)
    On Error Goto L_BREAK
    obj.Left = cel.Left
    obj.Top = cel.Top
    obj.Width = cel.Width
    obj.Height = cel.Height
    obj.BackColor = cel.Interior.Color
    If ref_addr Then obj.LinkedCell = cel.Address()
L_BREAK:
    On Error Goto 0
End Function

Public Function ResetUI_Edit(ByRef ed as Object, ByRef cell as Object, Optional ByVal ref_addr As Boolean = False)
    ed.Value = ""
    Call obj_ref_cel(ed, cell, ref_addr)
End Function

' 重置 ComboBox Control 元件
Public Function ResetUI_ComboBox(ByRef cb as Object, ByVal options As String, Optional ByRef cell as Object, Optional ByVal ref_addr As Boolean = False)
    cb.Clear
    
    arr = Split(options, "|")
    For i = LBound(arr) to UBound(arr)
        cb.AddItem arr(i), i
    Next

    If UBound(arr)>0 Then cb.ListIndex = 0
    If False = IsMissing(cell) Then Call obj_ref_cel(cb, cell, ref_addr)
End Function

Public Function ResetUI_ComboBoxBuy(ByRef cb as Object, ByRef cel as Object)
    Call ResetUI_ComboBox(cb, "買入|賣出|先賣", cel)
End Function

Public Function ResetUI_ComboBoxOrdType(ByRef cb as Object, ByRef cel as Object)
    Call ResetUI_ComboBox(cb, "整股現股|整股融資|整股融券|零股|定盤現股|定盤融資|定盤融券", cel)
End Function

Public Function ResetUI_ComboBoxPriceType(ByRef cb As Object, ByRef cel As Object)
    Call ResetUI_ComboBox(cb, "限價|市價|漲停|跌停", cel)
End Function

Public Function ResetUI_ComboBoxOrdKnd(ByRef cb As Object, ByRef cel As Object)
    Call ResetUI_ComboBox(cb, "ROD|IOC|FOK", cel, True)
End Function

Public Function ResetUI_ComboBoxPreOrder(ByRef cb As Object, ByRef cel As Object)
    Call ResetUI_ComboBox(cb, "非預約單|預約單", cel)
End Function

Public Function ResetUI_ComboBoxFoBuy(ByRef cb As Object, ByRef cel As Object)
    Call ResetUI_ComboBox(cb, "買入|賣出", cel)
End Function

Public Function ResetUI_ComboBoxFoPriceType(ByRef cb As Object, ByRef cel As Object)
    Call ResetUI_ComboBox(cb, "限價|市價|一定範圍市價", cel)
End Function

Public Function ResetUI_ComboBoxFoOctType(ByRef cb As Object, ByRef cel As Object)
    Call ResetUI_ComboBox(cb, "自動|新倉|平倉|當沖", cel)
End Function

Public Function ResetUI_ComboBoxOptComp(ByRef cb As Object, ByRef cel As Object)
    Call ResetUI_ComboBox(cb, "單式|複式", cel)
End Function

Public Function SetUI_ComboBoxAccounts(ByRef cb As Object, ByVal accounts as String, ByVal bStockOrFuOpt As Boolean)
    arr = Split(accounts, Chr(10))
    For i = LBound(arr) To UBound(arr)
        If IsStockBranch(arr(i)) And bStockOrFuOpt Then
            item = cb.AddItem(arr(i))
            If cb.ListIndex <0 then cb.ListIndex = item
        ElseIf IsFutOptBranch(arr(i)) And bStockOrFuOpt = False Then
            ' 期貨帳戶前多一個 F 不用
            account = Right(arr(i), Len(arr(i))-1)
            item = cb.AddItem(account)
            If cb.ListIndex <0 then cb.ListIndex = item
        End If
    Next
    
End Function
