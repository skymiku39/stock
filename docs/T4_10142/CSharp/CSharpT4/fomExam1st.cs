using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Data;
using System.Drawing;
using System.Linq;
using System.Text;
using System.Windows.Forms;

namespace CSharpT4
{
    public partial class fomExam1st : Form
    {
        public string userId;
        public string branch;
        public string account;
        public string content;

        public fomExam1st()
        {
            InitializeComponent();
        }

        private void fomExam1st_Load(object sender, EventArgs e)
        {
            cbFakeStockBS.SelectedIndex = 0;
            cbFakeStockOrderType.SelectedIndex = 0;
            cbFakeStockPriceType.SelectedIndex = 0;

            cbFakeFuBS.SelectedIndex = 0;
            cbFakeFuOrderType.SelectedIndex = 0;
            cbFakeFuPriceType.SelectedIndex = 0;
        }

        private void btnExam1st_Click(object sender, EventArgs e)
        {
            content = cbFakeStockBS.Text + ","
                + txFakeStockCode.Text + ","
                + txFakeStockPrice.Text + ","
                + txFakeStockQty.Text + ","
                + cbFakeStockPriceType.Text + ","
                + cbFakeStockOrderType.Text + ","
                + cbFakeFuBS.Text + ","
                + txFakeFuCode.Text + ","
                + txFakeFuPrice.Text + ","
                + txFakeFuQty.Text + ","
                + cbFakeFuPriceType.Text + ","
                + cbFakeFuOrderType.Text;

            userId = txUserID.Text;
            branch = txBranch.Text;
            account = txAccount.Text;

            this.DialogResult = DialogResult.OK;
            this.Close();
            //Form1.exam1st( txUserID.Text, txBranch.Text, txAccount.Text, content);
        }
    }
}
