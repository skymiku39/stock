Attribute VB_Name = "CO"

' 取得分公司代碼
Public Function GetBranch(ByVal account as String) As String
    GetBranch = Split(account, "-")(0)
End Function

' 取得帳戶
Public Function GetAccount(ByVal account as String) As String
    GetAccount = Split(account, "-")(1)
End Function

' 取得名字
Public Function GetAccountName(ByVal account as String) As String
    GetAccountName = Split(account, "-")(2)
End Function


' 檢查是否為證券帳戶
Public Function IsStockBranch(ByVal branch as String) As Boolean
    If "S" = Left(branch, 1) Then
        IsStockBranch = True
    Else
        IsStockBranch = False
    End If
End Function

' 檢查是否為期貨帳戶
Public Function IsFutOptBranch(ByVal branch As String) As Boolean
    If "F" = Left(branch, 1) Then
        IsFutOptBranch = True
    Else
        IsFutOptBranch = False
    End If
End Function


' 臺灣期貨交易所商品編碼 月年
Public Function GetTWFEMonYr(ByVal mon As Integer, ByVal yr As Integer) As String
    GetTWFEMonYr = Mid("ABCDEFGHIJKL", mon, 1) + Right(Str(yr), 1)
End Function

' 日期是否為該月結算後 (每月第三個週三結算)
Public Function IsSettled(ByVal yr As Integer, ByVal mon As Integer, ByVal day As Integer) As Boolean
    w = DateTime.Weekday(Str(yr) + "/" + Str(mon) + "/01")
    
    For i = 1 to day
        If w = 4 Then j = j + 1
        w = w + 1
        If w > 7 then w = 1
    Next
    
    If j >= 3 Then
        IsSettled = True
    Else
        IsSettled = False
    End If
End Function

' 臺灣期貨交易所近月商品 
Public Function GetTWFEPromptMonYr() As String
    tday = Now
    m1 = DateTime.Month(tday)
    y1 = DateTime.Year(tday)
    d1 = DateTime.Day(tday)
    
    If IsSettled(y1, m1, d1) Then
        m1 = m1 + 1
        If m1 > 12 Then
            m1 = 1
            y1 = y1 + 1
        End If
    End If
    
    GetTWFEPromptMonYr = GetTWFEMonYr( m1, y1 )
End Function



